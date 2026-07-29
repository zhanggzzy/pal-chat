from __future__ import annotations

import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import httpx
from conftest import LiveServer
from pal_chat_server.db import get_session_factory
from pal_chat_server.models import ConversationRecord
from pal_chat_server.schemas import ExperimentProfile


def default_profile_payload(client: httpx.Client) -> dict[str, Any]:
    response = client.post("/api/v1/profile-templates/default-natural-chat-v1/clone")
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["profile"])


def configure_runtime(
    server: LiveServer,
    *,
    script: list[dict[str, Any]],
    timing: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    worker_restart_limit: int | None = None,
) -> str:
    profile = default_profile_payload(server.client)
    modules = cast(dict[str, dict[str, Any]], profile["modules"])
    modules["model_adapter"] = {
        "module_id": "model.scripted",
        "config": {"script": script},
    }
    modules["timing"]["config"] = timing or {}
    if budget is not None:
        modules["budget"]["config"] = budget
    profile_metadata = cast(dict[str, Any], profile["metadata"])
    profile_metadata["phase"] = 3
    profile_metadata["agent_runtime_enabled"] = True
    created = server.client.post(
        "/api/v1/conversations",
        json={"title": "Phase 3 Process Runtime", "draft_profile": profile},
    )
    assert created.status_code == 201
    conversation_id = cast(str, created.json()["id"])
    if worker_restart_limit is not None:
        patched = server.client.patch(
            f"/api/v1/conversations/{conversation_id}/guardrails",
            json={"worker_restart_limit": worker_restart_limit},
        )
        assert patched.status_code == 200
    validate = server.client.post(f"/api/v1/conversations/{conversation_id}/validate")
    assert validate.status_code == 200
    started = server.client.post(f"/api/v1/conversations/{conversation_id}/start")
    assert started.status_code == 200
    return conversation_id


def create_conversation(client: httpx.Client) -> str:
    profile = default_profile_payload(client)
    created = client.post(
        "/api/v1/conversations",
        json={"title": "Path Isolation Probe", "draft_profile": profile},
    )
    assert created.status_code == 201
    return cast(str, created.json()["id"])


def submit_user_message(
    client: httpx.Client,
    conversation_id: str,
    *,
    client_message_id: str,
    content_markdown: str,
    mentions: list[str],
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={
            "client_message_id": client_message_id,
            "content_markdown": content_markdown,
            "mentions": mentions,
            "responds_to": [],
            "primary_reply_to": None,
        },
    )
    assert response.status_code == 201
    return cast(dict[str, Any], response.json())


def load_record_and_profile(conversation_id: str) -> tuple[ConversationRecord, ExperimentProfile]:
    with get_session_factory()() as session:
        record = session.get(ConversationRecord, conversation_id)
        assert record is not None
        profile = ExperimentProfile.model_validate(record.locked_profile_json)
        return record, profile


def transcript_path(conversation_id: str) -> Path:
    record, _ = load_record_and_profile(conversation_id)
    assert record.archive_dir is not None
    return Path(record.archive_dir) / "transcript.sqlite"


def fetch_rows(conversation_id: str, query: str) -> list[sqlite3.Row]:
    connection = sqlite3.connect(transcript_path(conversation_id))
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query).fetchall()
    finally:
        connection.close()


def agent_messages(client: httpx.Client, conversation_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert response.status_code == 200
    items = cast(list[dict[str, Any]], response.json()["items"])
    return [item for item in items if item["sender_kind"] == "agent"]


def wait_until(
    predicate: Any,
    *,
    timeout: float = 10,
    interval: float = 0.05,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition not met before timeout")


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def worker_snapshot(server: LiveServer, conversation_id: str) -> dict[str, dict[str, Any]]:
    supervisor = server.app.state.worker_supervisor
    record, profile = load_record_and_profile(conversation_id)
    snapshot = supervisor.runtime_snapshot(record, profile=profile)
    return {item["agent_id"]: item for item in snapshot["workers"]}


def auth_headers(server: LiveServer, conversation_id: str, agent_id: str) -> dict[str, str]:
    supervisor = server.app.state.worker_supervisor
    handle = supervisor._handles[conversation_id][agent_id]
    return {
        "Authorization": f"Bearer {handle.token}",
        "X-Agent-ID": agent_id,
        "X-Profile-Hash": handle.profile_hash,
    }


def run_statuses(conversation_id: str) -> list[str]:
    return [
        row["status"]
        for row in fetch_rows(
            conversation_id,
            "SELECT status FROM agent_runs ORDER BY started_at, agent_id",
        )
    ]


def agent_runs(conversation_id: str) -> list[sqlite3.Row]:
    return fetch_rows(
        conversation_id,
        """
        SELECT agent_id, status, phase, causal_episode_id, caused_by_message_id,
               agent_hop, finished_at
        FROM agent_runs
        ORDER BY started_at, agent_id
        """,
    )


def typing_events(conversation_id: str) -> list[str]:
    return [
        row["event_type"]
        for row in fetch_rows(
            conversation_id,
            """
            SELECT event_type
            FROM outbox_events
            WHERE event_type IN ('agent.typing_started', 'agent.typing_stopped')
            ORDER BY created_at
            """,
        )
    ]


def test_h03_live_direct_mention_obligates_single_agent(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "output_json": {"should_reply": True, "reason_codes": ["direct_mention"]},
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "output_json": {
                    "content_markdown": "A 收到并响应",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
    )
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="h03-user",
        content_markdown="@A 你来答",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)

    messages = agent_messages(live_process_server.client, conversation_id)
    assert len(messages) == 1
    assert messages[0]["sender_id"] == "agent-a"
    wait_until(lambda: run_statuses(conversation_id) == ["COMMITTED"])


def test_h05_live_all_mention_keeps_both_agent_obligations(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "output_json": {"should_reply": True},
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "output_json": {
                    "content_markdown": "A 先补充",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
            {
                "purpose": "decision",
                "agent_id": "agent-b",
                "output_json": {"should_reply": True},
            },
            {
                "purpose": "action",
                "agent_id": "agent-b",
                "output_json": {
                    "content_markdown": "B 补充另一角度",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
    )
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="h05-user",
        content_markdown="@all 一起回答",
        mentions=["agent-a", "agent-b"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 2)
    wait_until(lambda: run_statuses(conversation_id) == ["COMMITTED", "COMMITTED"])

    messages = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/messages"
    )
    assert messages.status_code == 200
    items = cast(list[dict[str, Any]], messages.json()["items"])
    assert [item["conversation_seq"] for item in items] == [1, 2, 3]
    assert {item["sender_id"] for item in items[1:]} == {"agent-a", "agent-b"}


def test_h06_live_interruption_invalidates_stale_draft_and_cancels_typing(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "call_index": 1,
                "delay_ms": 200,
                "output_json": {"should_reply": True},
            },
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "call_index": 2,
                "output_json": {"should_reply": True},
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "output_json": {
                    "content_markdown": "只保留新的回复",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
    )
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="h06-user-1",
        content_markdown="@A 先看这个",
        mentions=["agent-a"],
    )
    wait_until(lambda: run_statuses(conversation_id) == ["RUNNING"])
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="h06-user-2",
        content_markdown="@A 等等，改成这个",
        mentions=["agent-a"],
    )
    wait_until(
        lambda: [
            item["content_markdown"]
            for item in agent_messages(live_process_server.client, conversation_id)
        ]
        == ["只保留新的回复"]
    )
    wait_until(lambda: run_statuses(conversation_id) == ["INVALIDATED", "COMMITTED"])
    assert typing_events(conversation_id) == [
        "agent.typing_started",
        "agent.typing_stopped",
        "agent.typing_started",
        "agent.typing_stopped",
    ]


def test_h07_live_episode_budget_stops_after_four_two_two(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@all 接龙",
                "output_json": {"should_reply": True, "reply_key": "a1"},
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"a1\"",
                "output_json": {
                    "content_markdown": "A 接力 1",
                    "mentions": ["agent-b"],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
            {
                "purpose": "decision",
                "agent_id": "agent-b",
                "match_contains": "@all 接龙",
                "output_json": {"should_reply": True, "reply_key": "b1"},
            },
            {
                "purpose": "action",
                "agent_id": "agent-b",
                "match_contains": "\"reply_key\": \"b1\"",
                "output_json": {
                    "content_markdown": "B 接力 1",
                    "mentions": ["agent-a"],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "B 接力 1",
                "output_json": {"should_reply": True, "reply_key": "a2"},
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"a2\"",
                "output_json": {
                    "content_markdown": "A 接力 2",
                    "mentions": ["agent-b"],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
            {
                "purpose": "decision",
                "agent_id": "agent-b",
                "match_contains": "A 接力 2",
                "output_json": {"should_reply": True, "reply_key": "b2"},
            },
            {
                "purpose": "action",
                "agent_id": "agent-b",
                "match_contains": "\"reply_key\": \"b2\"",
                "output_json": {
                    "content_markdown": "B 接力 2",
                    "mentions": ["agent-a"],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "B 接力 2",
                "output_json": {"should_reply": True, "reply_key": "a3"},
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"a3\"",
                "output_json": {
                    "content_markdown": "A 不应再发",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
        budget={
            "max_agent_actions_per_episode": 4,
            "max_actions_per_agent_per_episode": 2,
            "max_consecutive_agent_hops": 2,
        },
    )
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="h07-user",
        content_markdown="@all 接龙",
        mentions=["agent-a", "agent-b"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 4)
    wait_until(
        lambda: [row["status"] for row in agent_runs(conversation_id)].count("COMMITTED") == 4
        and [row["status"] for row in agent_runs(conversation_id)].count("BUDGET_EXHAUSTED")
        == 1
        and all(row["finished_at"] is not None for row in agent_runs(conversation_id))
    )

    messages = agent_messages(live_process_server.client, conversation_id)
    assert {item["content_markdown"] for item in messages} == {
        "A 接力 1",
        "B 接力 1",
        "A 接力 2",
        "B 接力 2",
    }
    assert "A 不应再发" not in {item["content_markdown"] for item in messages}
    budget_rows = fetch_rows(
        conversation_id,
        """
        SELECT total_actions, agent_a_actions, agent_b_actions, max_agent_hop
        FROM causal_episode_budget
        """,
    )
    assert len(budget_rows) == 1
    row = budget_rows[0]
    assert row["total_actions"] == 4
    assert row["agent_a_actions"] == 2
    assert row["agent_b_actions"] == 2
    assert row["max_agent_hop"] == 2
    assert all(
        message["causal_episode_id"] == messages[0]["causal_episode_id"]
        for message in messages
    )
    assert sorted(message["agent_hop"] for message in messages) == [0, 0, 1, 2]
    runs = [dict(row) for row in agent_runs(conversation_id)]
    exhausted = [row for row in runs if row["status"] == "BUDGET_EXHAUSTED"]
    assert len(exhausted) == 1
    assert exhausted[0]["agent_id"] == "agent-a"
    assert exhausted[0]["agent_hop"] == 3
    assert exhausted[0]["causal_episode_id"] == messages[0]["causal_episode_id"]
    message_count = fetch_rows(
        conversation_id,
        "SELECT COUNT(*) AS count FROM messages",
    )[0]["count"]
    assert message_count == 5


def test_pause_checkpoint_ack_clears_running_trace(live_process_server: LiveServer) -> None:
    conversation_id = configure_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "delay_ms": 200,
                "output_json": {"should_reply": True},
            }
        ],
    )
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="pause-user",
        content_markdown="@A 先别发",
        mentions=["agent-a"],
    )
    wait_until(lambda: run_statuses(conversation_id) == ["RUNNING"])
    paused = live_process_server.client.post(f"/api/v1/conversations/{conversation_id}/pause")
    assert paused.status_code == 200
    wait_until(lambda: run_statuses(conversation_id) == ["INVALIDATED"])
    snapshot = worker_snapshot(live_process_server, conversation_id)
    assert snapshot["agent-a"]["pause_ack"] is True
    wait_until(
        lambda: [
            (row["agent_id"], row["worker_state"], row["active_run_id"])
            for row in fetch_rows(
                conversation_id,
                """
                SELECT agent_id, worker_state, active_run_id
                FROM agent_runtime_state
                ORDER BY agent_id
                """,
            )
        ]
        == [("agent-a", "PAUSED", None), ("agent-b", "PAUSED", None)]
    )
    resumed = live_process_server.client.post(f"/api/v1/conversations/{conversation_id}/resume")
    assert resumed.status_code == 200
    wait_until(
        lambda: {
            row["worker_state"]
            for row in fetch_rows(
                conversation_id,
                "SELECT worker_state FROM agent_runtime_state",
            )
        }
        == {"LISTENING"}
    )


def test_real_worker_auth_isolation_revocation_and_path_escape(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_runtime(live_process_server, script=[])
    other_conversation_id = create_conversation(live_process_server.client)
    snapshot = worker_snapshot(live_process_server, conversation_id)
    assert set(snapshot) == {"agent-a", "agent-b"}
    assert snapshot["agent-a"]["pid"] != snapshot["agent-b"]["pid"]

    headers = auth_headers(live_process_server, conversation_id, "agent-a")
    no_auth = live_process_server.client.get(
        f"/internal/v1/conversations/{conversation_id}/runtime-snapshot"
    )
    assert no_auth.status_code == 401

    bad_token = live_process_server.client.get(
        f"/internal/v1/conversations/{conversation_id}/runtime-snapshot",
        headers={**headers, "Authorization": "Bearer bad-token"},
    )
    assert bad_token.status_code == 401

    wrong_agent = live_process_server.client.get(
        f"/internal/v1/conversations/{conversation_id}/runtime-snapshot",
        headers={**headers, "X-Agent-ID": "agent-b"},
    )
    assert wrong_agent.status_code == 403

    wrong_profile = live_process_server.client.get(
        f"/internal/v1/conversations/{conversation_id}/runtime-snapshot",
        headers={**headers, "X-Profile-Hash": "wrong-profile"},
    )
    assert wrong_profile.status_code == 403

    wrong_path = live_process_server.client.get(
        f"/internal/v1/conversations/{other_conversation_id}/runtime-snapshot",
        headers=headers,
    )
    assert wrong_path.status_code == 403

    escaped_path = quote(f"../{conversation_id}", safe="")
    escaped = live_process_server.client.get(
        f"/internal/v1/conversations/{escaped_path}/runtime-snapshot",
        headers=headers,
    )
    assert escaped.status_code in {404, 422}

    pids = [int(worker["pid"]) for worker in snapshot.values()]
    assert live_process_server.client.post(
        f"/api/v1/conversations/{conversation_id}/end"
    ).status_code == 200
    wait_until(lambda: all(not process_alive(pid) for pid in pids))

    revoked = live_process_server.client.get(
        f"/internal/v1/conversations/{conversation_id}/runtime-snapshot",
        headers=headers,
    )
    assert revoked.status_code == 401


def test_real_worker_restarts_after_external_kill(live_process_server: LiveServer) -> None:
    conversation_id = configure_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "output_json": {"should_reply": True},
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "output_json": {
                    "content_markdown": "重启后恢复响应",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
        worker_restart_limit=1,
    )
    initial_pid = int(worker_snapshot(live_process_server, conversation_id)["agent-a"]["pid"])
    os.kill(initial_pid, signal.SIGTERM)

    def restarted() -> bool:
        worker = worker_snapshot(live_process_server, conversation_id)["agent-a"]
        return (
            int(worker["restart_count"]) == 1
            and int(worker["pid"]) != initial_pid
            and bool(worker["connected"])
        )

    wait_until(restarted)
    restarted_pid = int(worker_snapshot(live_process_server, conversation_id)["agent-a"]["pid"])
    assert restarted_pid != initial_pid

    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="restart-user",
        content_markdown="@A 重启后继续",
        mentions=["agent-a"],
    )
    wait_until(
        lambda: [
            message["content_markdown"]
            for message in agent_messages(live_process_server.client, conversation_id)
        ]
        == ["重启后恢复响应"]
    )

    assert live_process_server.client.post(
        f"/api/v1/conversations/{conversation_id}/end"
    ).status_code == 200
    wait_until(lambda: not process_alive(initial_pid) and not process_alive(restarted_pid))


def test_real_worker_fatal_exit_does_not_restart(live_process_server: LiveServer) -> None:
    conversation_id = configure_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "fail_code": "WORKER_CRASHED_FATAL",
            }
        ],
    )
    initial_pid = int(worker_snapshot(live_process_server, conversation_id)["agent-a"]["pid"])
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="fatal-user",
        content_markdown="@A 直接失败",
        mentions=["agent-a"],
    )
    wait_until(lambda: not process_alive(initial_pid))
    wait_until(lambda: run_statuses(conversation_id) == ["FATAL"])

    snapshot = worker_snapshot(live_process_server, conversation_id)["agent-a"]
    assert snapshot["pid"] == initial_pid
    assert snapshot["alive"] is False
    assert snapshot["restart_count"] == 0
    assert agent_messages(live_process_server.client, conversation_id) == []
