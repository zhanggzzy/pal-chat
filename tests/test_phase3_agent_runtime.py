from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from pal_chat_server.agent_runtime import AgentRuntimeManager
from pal_chat_server.clock import VirtualClock
from pal_chat_server.db import get_session_factory
from pal_chat_server.models import ConversationRecord
from pal_chat_server.schemas import ExperimentProfile


def default_profile_payload(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/v1/profile-templates/default-natural-chat-v1/clone")
    assert response.status_code == 200
    return cast(dict[str, Any], response.json()["profile"])


def configure_runtime(
    client: TestClient,
    *,
    script: list[dict[str, Any]],
    timing: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, AgentRuntimeManager]:
    manager = AgentRuntimeManager(
        clock=VirtualClock(),
        auto_pump=False,
    )
    client.app.state.runtime_manager = manager
    profile = default_profile_payload(client)
    modules = cast(dict[str, dict[str, Any]], profile["modules"])
    modules["model_adapter"] = {
        "module_id": "model.scripted",
        "config": {"script": script},
    }
    modules["timing"]["config"] = timing or {}
    modules["budget"]["config"] = budget or {}
    profile_metadata = cast(dict[str, Any], profile["metadata"])
    profile_metadata["phase"] = 3
    profile_metadata["agent_runtime_enabled"] = True
    profile_metadata.update(metadata or {})
    created = client.post(
        "/api/v1/conversations",
        json={"title": "Phase 3 Runtime Demo", "draft_profile": profile},
    )
    assert created.status_code == 201
    conversation_id = cast(str, created.json()["id"])
    assert client.post(f"/api/v1/conversations/{conversation_id}/validate").status_code == 200
    assert client.post(f"/api/v1/conversations/{conversation_id}/start").status_code == 200
    return conversation_id, manager


def submit_user_message(
    client: TestClient,
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


def agent_messages(client: TestClient, conversation_id: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/v1/conversations/{conversation_id}/messages")
    assert response.status_code == 200
    items = cast(list[dict[str, Any]], response.json()["items"])
    return [
        item
        for item in items
        if item["sender_kind"] == "agent"
    ]


def test_h03_direct_mention_obligates_single_agent(migrated_app: TestClient) -> None:
    conversation_id, manager = configure_runtime(
        migrated_app,
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
        migrated_app,
        conversation_id,
        client_message_id="h03-user",
        content_markdown="@A 你来答",
        mentions=["agent-a"],
    )
    record, profile = load_record_and_profile(conversation_id)
    manager.pump(record, profile=profile)

    messages = agent_messages(migrated_app, conversation_id)
    assert len(messages) == 1
    assert messages[0]["sender_id"] == "agent-a"
    snapshot = manager.runtime_snapshot(record, profile=profile)
    workers = {item["agent_id"]: item for item in snapshot["workers"]}
    assert workers["agent-a"]["active_run_id"] is None
    assert workers["agent-b"]["active_run_id"] is None


def test_h05_all_mention_keeps_both_agent_obligations(migrated_app: TestClient) -> None:
    conversation_id, manager = configure_runtime(
        migrated_app,
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
                    "mentions": ["agent-b"],
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
        timing={"base_wait_ms": 50},
    )
    submit_user_message(
        migrated_app,
        conversation_id,
        client_message_id="h05-user",
        content_markdown="@all 一起回答",
        mentions=["agent-a", "agent-b"],
    )
    record, profile = load_record_and_profile(conversation_id)
    manager.pump(record, profile=profile)
    manager.advance(record, profile=profile, delta_ms=50)
    manager.advance(record, profile=profile, delta_ms=50)

    messages = agent_messages(migrated_app, conversation_id)
    assert [item["sender_id"] for item in messages] == ["agent-a", "agent-b"]
    runs = fetch_rows(
        conversation_id,
        "SELECT agent_id, status FROM agent_runs ORDER BY started_at, agent_id",
    )
    statuses = [row["status"] for row in runs]
    assert statuses.count("COMMITTED") == 2
    assert statuses.count("INVALIDATED") == 1


def test_h06_interruption_invalidates_stale_draft_and_cancels_typing(
    migrated_app: TestClient,
) -> None:
    conversation_id, manager = configure_runtime(
        migrated_app,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "call_index": 1,
                "delay_ms": 100,
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
        migrated_app,
        conversation_id,
        client_message_id="h06-user-1",
        content_markdown="@A 先看这个",
        mentions=["agent-a"],
    )
    record, profile = load_record_and_profile(conversation_id)
    manager.pump(record, profile=profile)
    submit_user_message(
        migrated_app,
        conversation_id,
        client_message_id="h06-user-2",
        content_markdown="@A 等等，改成这个",
        mentions=["agent-a"],
    )
    manager.advance(record, profile=profile, delta_ms=100)

    messages = agent_messages(migrated_app, conversation_id)
    assert [item["content_markdown"] for item in messages] == ["只保留新的回复"]
    runs = fetch_rows(
        conversation_id,
        "SELECT status, error_code FROM agent_runs ORDER BY started_at",
    )
    assert [row["status"] for row in runs] == ["INVALIDATED", "COMMITTED"]
    typing_events = fetch_rows(
        conversation_id,
        """
        SELECT event_type
        FROM outbox_events
        WHERE event_type IN ('agent.typing_started', 'agent.typing_stopped')
        ORDER BY created_at
        """,
    )
    assert [row["event_type"] for row in typing_events] == [
        "agent.typing_started",
        "agent.typing_stopped",
        "agent.typing_started",
        "agent.typing_stopped",
    ]


def test_h07_episode_budget_stops_after_four_two_two(migrated_app: TestClient) -> None:
    conversation_id, manager = configure_runtime(
        migrated_app,
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
                    "content_markdown": "A 接力",
                    "mentions": ["agent-b"],
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
                    "content_markdown": "B 接力",
                    "mentions": ["agent-a"],
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
        migrated_app,
        conversation_id,
        client_message_id="h07-user",
        content_markdown="@all 接龙",
        mentions=["agent-a", "agent-b"],
    )
    record, profile = load_record_and_profile(conversation_id)
    manager.pump(record, profile=profile)

    messages = agent_messages(migrated_app, conversation_id)
    assert len(messages) == 4
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
    assert row["max_agent_hop"] <= 2


def test_worker_crash_recovery_and_fatal_error(migrated_app: TestClient) -> None:
    recover_id, recover_manager = configure_runtime(
        migrated_app,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "call_index": 1,
                "fail_code": "WORKER_CRASHED",
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
                    "content_markdown": "恢复后继续",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
        metadata={"worker_restart_limit": 1},
    )
    submit_user_message(
        migrated_app,
        recover_id,
        client_message_id="recover-user",
        content_markdown="@A 崩了再恢复",
        mentions=["agent-a"],
    )
    record, profile = load_record_and_profile(recover_id)
    recover_manager.pump(record, profile=profile)
    recover_messages = agent_messages(migrated_app, recover_id)
    assert [item["content_markdown"] for item in recover_messages] == ["恢复后继续"]
    snapshot = recover_manager.runtime_snapshot(record, profile=profile)
    workers = {item["agent_id"]: item for item in snapshot["workers"]}
    assert workers["agent-a"]["restart_count"] == 1
    assert workers["agent-a"]["worker_state"] == "LISTENING"
    assert migrated_app.post(f"/api/v1/conversations/{recover_id}/end").status_code == 200

    fatal_id, fatal_manager = configure_runtime(
        migrated_app,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "fail_code": "WORKER_CRASHED_FATAL",
            }
        ],
    )
    submit_user_message(
        migrated_app,
        fatal_id,
        client_message_id="fatal-user",
        content_markdown="@A 直接失败",
        mentions=["agent-a"],
    )
    fatal_record, fatal_profile = load_record_and_profile(fatal_id)
    fatal_manager.pump(fatal_record, profile=fatal_profile)
    fatal_snapshot = fatal_manager.runtime_snapshot(
        fatal_record,
        profile=fatal_profile,
    )
    fatal_workers = {item["agent_id"]: item for item in fatal_snapshot["workers"]}
    assert fatal_workers["agent-a"]["worker_state"] == "ERROR"
    assert agent_messages(migrated_app, fatal_id) == []
