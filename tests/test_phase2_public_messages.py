from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any, cast

from fastapi.testclient import TestClient
from pal_chat_server.db import get_session_factory
from pal_chat_server.errors import AppError
from pal_chat_server.schemas import ExperimentProfile
from pal_chat_server.sequence_runtime import (
    MessagePayload,
    commit_message,
    connect_transcript,
    dispatch_pending_outbox,
)
from pal_chat_server.services import get_conversation_or_404


def default_profile_payload(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/v1/profile-templates/default-natural-chat-v1/clone")
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["profile"])


def create_running_conversation(
    client: TestClient,
    *,
    title: str = "Phase 2 Demo",
    fixed_window: bool = False,
) -> dict[str, Any]:
    profile = default_profile_payload(client)
    if fixed_window:
        modules = cast(dict[str, dict[str, Any]], profile["modules"])
        modules["projection"] = {
            "module_id": "projection.fixed-window",
            "config": {"window_size": 1},
        }
    created = client.post(
        "/api/v1/conversations",
        json={"title": title, "draft_profile": profile},
    )
    assert created.status_code == 201
    conversation = cast(dict[str, Any], created.json())
    conversation_id = cast(str, conversation["id"])
    assert client.post(f"/api/v1/conversations/{conversation_id}/validate").status_code == 200
    started = client.post(f"/api/v1/conversations/{conversation_id}/start")
    assert started.status_code == 200
    return cast(dict[str, Any], started.json())


def submit_message(
    client: TestClient,
    conversation_id: str,
    *,
    client_message_id: str,
    content_markdown: str,
    mentions: list[str] | None = None,
    primary_reply_to: str | None = None,
    responds_to: list[str] | None = None,
) -> Any:
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "client_message_id": client_message_id,
            "content_markdown": content_markdown,
            "mentions": mentions or [],
            "primary_reply_to": primary_reply_to,
            "responds_to": responds_to or [],
        },
    )


def load_record_and_profile(
    conversation_id: str,
) -> tuple[Any, ExperimentProfile]:
    session_factory = get_session_factory()
    with session_factory() as db:
        record = get_conversation_or_404(db, conversation_id)
        profile = ExperimentProfile.model_validate(record.locked_profile_json)
        db.expunge(record)
    return record, profile


def test_h01_duplicate_message_request_is_idempotent(migrated_app: TestClient) -> None:
    conversation = create_running_conversation(migrated_app)
    conversation_id = cast(str, conversation["id"])

    first = submit_message(
        migrated_app,
        conversation_id,
        client_message_id="dup-001",
        content_markdown="hello public channel",
    )
    duplicate = submit_message(
        migrated_app,
        conversation_id,
        client_message_id="dup-001",
        content_markdown="hello public channel",
    )

    assert first.status_code == 201
    assert duplicate.status_code == 201
    first_payload = first.json()
    duplicate_payload = duplicate.json()
    assert duplicate_payload["message"]["message_id"] == first_payload["message"]["message_id"]
    assert duplicate_payload["message"]["conversation_seq"] == 1

    reply = submit_message(
        migrated_app,
        conversation_id,
        client_message_id="reply-001",
        content_markdown="replying to seq1",
        mentions=["agent-a"],
        primary_reply_to=first_payload["message"]["message_id"],
        responds_to=[first_payload["message"]["message_id"]],
    )
    assert reply.status_code == 201
    reply_payload = reply.json()
    assert reply_payload["message"]["mentions"] == ["agent-a"]
    assert reply_payload["message"]["primary_reply_to"] == first_payload["message"]["message_id"]
    assert reply_payload["message"]["responds_to"] == [first_payload["message"]["message_id"]]

    messages = migrated_app.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    items = messages.json()["items"]
    assert [item["conversation_seq"] for item in items] == [1, 2]

    cp_revisions = migrated_app.get(f"/api/v1/conversations/{conversation_id}/cp-revisions")
    assert cp_revisions.status_code == 200
    revisions = cp_revisions.json()["items"]
    assert (
        revisions[-1]["projection_revision"]
        == reply_payload["cp_revision"]["projection_revision"]
    )
    assert revisions[-1]["covered_through_seq"] == 2


def test_h02_concurrent_commits_keep_contiguous_sequence(migrated_app: TestClient) -> None:
    conversation = create_running_conversation(
        migrated_app,
        title="Concurrent Demo",
        fixed_window=True,
    )
    conversation_id = cast(str, conversation["id"])
    barrier = Barrier(6)

    def worker(index: int) -> dict[str, Any]:
        barrier.wait()
        response = submit_message(
            migrated_app,
            conversation_id,
            client_message_id=f"concurrent-{index}",
            content_markdown=f"message-{index}",
        )
        assert response.status_code == 201
        return cast(dict[str, Any], response.json())

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))

    sequences = sorted(item["message"]["conversation_seq"] for item in results)
    assert sequences == [1, 2, 3, 4, 5, 6]

    messages = migrated_app.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    items = messages.json()["items"]
    assert [item["conversation_seq"] for item in items] == [1, 2, 3, 4, 5, 6]

    cp_revisions = migrated_app.get(f"/api/v1/conversations/{conversation_id}/cp-revisions")
    revisions = cp_revisions.json()["items"]
    assert len(revisions) == 6
    assert all(
        len(revision["snapshot"]["segments"]) == revision["projection_revision"]
        for revision in revisions
    )


def test_h12_failed_projection_does_not_consume_sequence(migrated_app: TestClient) -> None:
    profile = default_profile_payload(migrated_app)
    modules = cast(dict[str, dict[str, Any]], profile["modules"])
    modules["projection"] = {
        "module_id": "projection.segment-chain",
        "config": {
            "script": [
                {
                    "match_contains": "should not commit",
                    "operation": "START_NEW_SEGMENT",
                    "fail": True,
                }
            ]
        },
    }
    created = migrated_app.post(
        "/api/v1/conversations",
        json={"title": "Failure Demo", "draft_profile": profile},
    )
    assert created.status_code == 201
    conversation = cast(dict[str, Any], created.json())
    conversation_id = cast(str, conversation["id"])
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/validate").status_code == 200
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/start").status_code == 200

    failed = submit_message(
        migrated_app,
        conversation_id,
        client_message_id="fail-001",
        content_markdown="should not commit",
    )
    assert failed.status_code == 409
    assert failed.json()["error"]["code"] == "projection_failed"

    empty_messages = migrated_app.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert empty_messages.status_code == 200
    assert empty_messages.json()["items"] == []

    empty_cp = migrated_app.get(f"/api/v1/conversations/{conversation_id}/cp-revisions")
    assert empty_cp.status_code == 200
    assert empty_cp.json()["items"] == []

    retry_failed = migrated_app.post(
        f"/api/v1/conversations/{conversation_id}/submissions/fail-001/retry"
    )
    assert retry_failed.status_code == 409
    assert retry_failed.json()["error"]["code"] == "projection_failed"

    success = submit_message(
        migrated_app,
        conversation_id,
        client_message_id="ok-001",
        content_markdown="first committed message",
    )
    assert success.status_code == 201
    payload = success.json()
    assert payload["message"]["conversation_seq"] == 1
    assert payload["cp_revision"]["projection_revision"] == 1


def test_h13_reconnect_replays_missing_events_without_duplicate_dispatch(
    migrated_app: TestClient,
) -> None:
    conversation = create_running_conversation(migrated_app, title="Reconnect Demo")
    conversation_id = cast(str, conversation["id"])

    first = submit_message(
        migrated_app,
        conversation_id,
        client_message_id="ws-001",
        content_markdown="first",
    )
    second = submit_message(
        migrated_app,
        conversation_id,
        client_message_id="ws-002",
        content_markdown="second",
    )
    assert first.status_code == 201
    assert second.status_code == 201

    session_factory = get_session_factory()
    with session_factory() as db:
        record = get_conversation_or_404(db, conversation_id)
        assert dispatch_pending_outbox(record) == []

    with migrated_app.websocket_connect(
        f"/ws/v1/conversations/{conversation_id}?after_seq=0"
    ) as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["event_type"] == "session.snapshot"
        assert snapshot["payload"]["latest_conversation_seq"] == 2
        replayed = [websocket.receive_json() for _ in range(4)]

    replayed_ids = {event["event_id"] for event in replayed}
    assert len(replayed_ids) == 4
    replayed_by_seq = {
        seq: [event for event in replayed if event["conversation_seq"] == seq]
        for seq in (1, 2)
    }
    assert all(len(events) == 2 for events in replayed_by_seq.values())

    with migrated_app.websocket_connect(
        f"/ws/v1/conversations/{conversation_id}?after_seq=1"
    ) as websocket:
        snapshot = websocket.receive_json()
        assert snapshot["payload"]["latest_conversation_seq"] == 2
        missing_only = [websocket.receive_json() for _ in range(2)]

    assert {event["conversation_seq"] for event in missing_only} == {2}
    assert {event["event_id"] for event in missing_only} == {
        event["event_id"] for event in replayed_by_seq[2]
    }

    backfill = migrated_app.get(f"/api/v1/conversations/{conversation_id}/messages?after_seq=1")
    assert backfill.status_code == 200
    assert [item["conversation_seq"] for item in backfill.json()["items"]] == [2]


def test_projection_without_expected_seq_recomputes_after_stale_checkpoint(
    migrated_app: TestClient,
) -> None:
    profile = default_profile_payload(migrated_app)
    modules = cast(dict[str, dict[str, Any]], profile["modules"])
    modules["projection"] = {
        "module_id": "projection.segment-chain",
        "config": {
            "script": [
                {
                    "purpose": "projection",
                    "match_contains": "slow",
                    "delay_ms": 200,
                    "output_json": {
                        "operation": "START_NEW_SEGMENT",
                        "segment_title": "公共消息",
                        "updated_summary": "slow summary",
                    },
                }
            ]
        },
    }
    created = migrated_app.post(
        "/api/v1/conversations",
        json={"title": "Projection Recompute", "draft_profile": profile},
    )
    assert created.status_code == 201
    conversation_id = cast(str, created.json()["id"])
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/validate").status_code == 200
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/start").status_code == 200

    def slow_commit() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        record, locked_profile = load_record_and_profile(conversation_id)
        return commit_message(
            record,
            profile=locked_profile,
            payload=MessagePayload(
                client_message_id="recompute-slow",
                content_markdown="slow projection message",
                mentions=[],
                primary_reply_to=None,
                responds_to=[],
            ),
        )

    def fast_commit() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        time.sleep(0.05)
        record, locked_profile = load_record_and_profile(conversation_id)
        return commit_message(
            record,
            profile=locked_profile,
            payload=MessagePayload(
                client_message_id="recompute-fast",
                content_markdown="fast projection message",
                mentions=[],
                primary_reply_to=None,
                responds_to=[],
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        slow_future = pool.submit(slow_commit)
        fast_future = pool.submit(fast_commit)
        slow_result = slow_future.result()
        fast_result = fast_future.result()

    sequences = sorted([slow_result[0]["conversation_seq"], fast_result[0]["conversation_seq"]])
    assert sequences == [1, 2]

    messages = migrated_app.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    items = cast(list[dict[str, Any]], messages.json()["items"])
    assert [item["conversation_seq"] for item in items] == [1, 2]


def test_projection_with_expected_seq_fails_stale_without_public_commit(
    migrated_app: TestClient,
) -> None:
    profile = default_profile_payload(migrated_app)
    modules = cast(dict[str, dict[str, Any]], profile["modules"])
    modules["projection"] = {
        "module_id": "projection.segment-chain",
        "config": {
            "script": [
                {
                    "purpose": "projection",
                    "match_contains": "stale",
                    "delay_ms": 200,
                    "output_json": {
                        "operation": "START_NEW_SEGMENT",
                        "segment_title": "公共消息",
                        "updated_summary": "stale summary",
                    },
                }
            ]
        },
    }
    created = migrated_app.post(
        "/api/v1/conversations",
        json={"title": "Projection Expected Seq", "draft_profile": profile},
    )
    assert created.status_code == 201
    conversation_id = cast(str, created.json()["id"])
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/validate").status_code == 200
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/start").status_code == 200

    def stale_commit() -> AppError:
        record, locked_profile = load_record_and_profile(conversation_id)
        try:
            commit_message(
                record,
                profile=locked_profile,
                payload=MessagePayload(
                    client_message_id="stale-explicit",
                    content_markdown="stale candidate",
                    mentions=[],
                    primary_reply_to=None,
                    responds_to=[],
                    expected_conversation_seq=0,
                ),
            )
        except AppError as exc:
            return exc
        raise AssertionError("expected stale projection failure")

    def winner_commit() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        time.sleep(0.05)
        record, locked_profile = load_record_and_profile(conversation_id)
        return commit_message(
            record,
            profile=locked_profile,
            payload=MessagePayload(
                client_message_id="stale-winner",
                content_markdown="winner message",
                mentions=[],
                primary_reply_to=None,
                responds_to=[],
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_future = pool.submit(stale_commit)
        winner_future = pool.submit(winner_commit)
        stale_error = stale_future.result()
        winner_result = winner_future.result()

    assert stale_error.code == "projection_stale"
    assert winner_result[0]["conversation_seq"] == 1

    messages = migrated_app.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert messages.status_code == 200
    items = cast(list[dict[str, Any]], messages.json()["items"])
    assert [item["client_message_id"] for item in items] == ["stale-winner"]

    record, _ = load_record_and_profile(conversation_id)
    with connect_transcript(record) as connection:
        stale_submission = connection.execute(
            """
            SELECT status, error_code
            FROM submissions
            WHERE client_message_id = ?
            """,
            ("stale-explicit",),
        ).fetchone()
        assert stale_submission is not None
        assert stale_submission["status"] == "failed"
        assert stale_submission["error_code"] == "CP_STALE"


def test_projection_stale_retry_with_same_client_message_id_accepts_new_expected_seq(
    migrated_app: TestClient,
) -> None:
    profile = default_profile_payload(migrated_app)
    modules = cast(dict[str, dict[str, Any]], profile["modules"])
    modules["projection"] = {
        "module_id": "projection.segment-chain",
        "config": {
            "script": [
                {
                    "purpose": "projection",
                    "match_contains": "retry me",
                    "delay_ms": 200,
                    "output_json": {
                        "operation": "START_NEW_SEGMENT",
                        "segment_title": "公共消息",
                        "updated_summary": "retry summary",
                    },
                }
            ]
        },
    }
    created = migrated_app.post(
        "/api/v1/conversations",
        json={"title": "Projection Retry Same Client ID", "draft_profile": profile},
    )
    assert created.status_code == 201
    conversation_id = cast(str, created.json()["id"])
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/validate").status_code == 200
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/start").status_code == 200

    def stale_then_retry() -> tuple[
        AppError,
        tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]],
    ]:
        record, locked_profile = load_record_and_profile(conversation_id)
        try:
            commit_message(
                record,
                profile=locked_profile,
                payload=MessagePayload(
                    client_message_id="retry-same-client-id",
                    content_markdown="retry me later",
                    mentions=[],
                    primary_reply_to=None,
                    responds_to=[],
                    expected_conversation_seq=0,
                ),
            )
        except AppError as exc:
            retried_record, retried_profile = load_record_and_profile(conversation_id)
            retried = commit_message(
                retried_record,
                profile=retried_profile,
                payload=MessagePayload(
                    client_message_id="retry-same-client-id",
                    content_markdown="retry me later",
                    mentions=[],
                    primary_reply_to=None,
                    responds_to=[],
                    expected_conversation_seq=1,
                ),
            )
            return exc, retried
        raise AssertionError("expected initial projection_stale")

    def winner_commit() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        time.sleep(0.05)
        record, locked_profile = load_record_and_profile(conversation_id)
        return commit_message(
            record,
            profile=locked_profile,
            payload=MessagePayload(
                client_message_id="retry-winner",
                content_markdown="winner before retry",
                mentions=[],
                primary_reply_to=None,
                responds_to=[],
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        stale_future = pool.submit(stale_then_retry)
        winner_future = pool.submit(winner_commit)
        stale_error, retry_result = stale_future.result()
        winner_result = winner_future.result()

    assert stale_error.code == "projection_stale"
    assert winner_result[0]["conversation_seq"] == 1
    assert retry_result[0]["conversation_seq"] == 2
    assert retry_result[0]["client_message_id"] == "retry-same-client-id"


def test_projection_budget_rejection_uses_degraded_cp_without_attempt(
    migrated_app: TestClient,
) -> None:
    conversation = create_running_conversation(migrated_app, title="Projection Budget Reject")
    conversation_id = cast(str, conversation["id"])
    patched = migrated_app.patch(
        f"/api/v1/conversations/{conversation_id}/guardrails",
        json={"max_llm_calls": 0, "max_total_tokens": 0},
    )
    assert patched.status_code == 200

    response = submit_message(
        migrated_app,
        conversation_id,
        client_message_id="projection-budget",
        content_markdown="仍应提交的用户消息",
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["message"]["conversation_seq"] == 1
    assert payload["cp_revision"]["projection_revision"] == 1
    assert payload["cp_revision"]["snapshot"]["segments"][0]["summary"] == "仍应提交的用户消息"

    record, _ = load_record_and_profile(conversation_id)
    with connect_transcript(record) as connection:
        attempt_count = connection.execute(
            "SELECT COUNT(*) AS count FROM llm_attempts WHERE phase = 'projection'"
        ).fetchone()
        admission_row = connection.execute(
            """
            SELECT phase, decision, reason_code
            FROM llm_admission_events
            WHERE phase = 'projection'
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()
    assert attempt_count is not None and int(attempt_count["count"]) == 0
    assert admission_row is not None
    assert tuple(admission_row) == ("projection", "rejected", "budget_exhausted")
