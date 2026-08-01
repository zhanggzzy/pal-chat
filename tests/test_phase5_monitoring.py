from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, cast

from conftest import LiveServer
from pal_chat_server.sequence_runtime import SOCKET_MANAGER
from pal_chat_server.worker_main import RawWebSocketClient
from test_phase3_worker_processes import agent_messages, submit_user_message, wait_until
from test_phase4_memory_context import archive_root, configure_phase4_runtime


def _phase5_conversation(server: LiveServer) -> str:
    return configure_phase4_runtime(
        server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@A",
                "output_json": {
                    "should_reply": True,
                    "reply_key": "answer",
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "upsert_node",
                                "node": {
                                    "memory_node_id": "belief-1",
                                    "type": "Belief",
                                    "content": "阶段 5 记忆",
                                    "confidence": 0.9,
                                    "salience": 0.8,
                                    "status": "open",
                                    "source_refs": ["phase5-user"],
                                },
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"answer\"",
                "output_json": {
                    "content_markdown": "A 已完成阶段 5 链路",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
    )


def _fetch_transcript_row(
    transcript: Path,
    query: str,
    params: tuple[object, ...] = (),
) -> sqlite3.Row:
    connection = sqlite3.connect(transcript)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(query, params).fetchone()
        assert row is not None
        return cast(sqlite3.Row, row)
    finally:
        connection.close()


def test_phase5_monitoring_endpoints_trace_run_memory_context_and_costs(
    live_process_server: LiveServer,
) -> None:
    conversation_id = _phase5_conversation(live_process_server)
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="phase5-user",
        content_markdown="@A 请记录并回答",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)

    runs_response = live_process_server.client.get(f"/api/v1/conversations/{conversation_id}/runs")
    assert runs_response.status_code == 200
    runs = cast(list[dict[str, Any]], runs_response.json()["items"])
    assert len(runs) == 1
    run = runs[0]
    assert run["agent_id"] == "agent-a"
    assert run["status"] == "COMMITTED"
    assert run["decision_json"]["bundle_revision"].startswith("ctx-")
    assert run["draft_message_json"]["bundle_revision"].startswith("ctx-")
    assert len(run["attempts"]) == 2
    assert all(attempt["total_tokens"] > 0 for attempt in run["attempts"])
    assert all(attempt["latency_ms"] is not None for attempt in run["attempts"])

    run_detail = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/runs/{run['run_id']}"
    )
    assert run_detail.status_code == 200
    assert run_detail.json()["run_id"] == run["run_id"]

    memory_revisions = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/agents/agent-a/memory/revisions"
    )
    assert memory_revisions.status_code == 200
    memory_items = cast(list[dict[str, Any]], memory_revisions.json()["items"])
    assert memory_items[0]["revision"] == "mem-1"
    assert any(
        node["content"] == "阶段 5 记忆"
        for node in memory_items[0]["payload"]["nodes"]
    )

    memory_detail = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/agents/agent-a/memory/revisions/mem-1"
    )
    assert memory_detail.status_code == 200
    assert memory_detail.json()["revision"] == "mem-1"

    context_bundles = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/agents/agent-a/context-bundles",
        params={"run_id": run["run_id"]},
    )
    assert context_bundles.status_code == 200
    bundle_items = cast(list[dict[str, Any]], context_bundles.json()["items"])
    assert [item["phase"] for item in bundle_items] == ["action", "decision"]

    attempts = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/agents/agent-a/attempts",
        params={"run_id": run["run_id"]},
    )
    assert attempts.status_code == 200
    attempt_items = cast(list[dict[str, Any]], attempts.json()["items"])
    assert [item["phase"] for item in attempt_items] == ["decision", "action"]
    assert all("cost_usd" in item for item in attempt_items)

    episodes = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/causal-episodes"
    )
    assert episodes.status_code == 200
    episode_items = cast(list[dict[str, Any]], episodes.json()["items"])
    assert len(episode_items) == 1
    assert episode_items[0]["total_actions"] == 1

    costs = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/metrics/cost-breakdown"
    )
    assert costs.status_code == 200
    cost_payload = cast(dict[str, Any], costs.json())
    assert cost_payload["total_tokens"] > 0
    assert "agent-a" in cost_payload["by_agent"]

    logs = live_process_server.client.get(f"/api/v1/conversations/{conversation_id}/logs")
    assert logs.status_code == 200
    log_items = cast(list[dict[str, Any]], logs.json()["items"])
    assert any(item["run_id"] == run["run_id"] for item in log_items)


def test_phase5_public_ws_emits_agent_and_guardrail_events(
    live_process_server: LiveServer,
) -> None:
    conversation_id = _phase5_conversation(live_process_server)
    ws_url = live_process_server.base_url.replace("http://", "ws://")
    ws = RawWebSocketClient(
        f"{ws_url}/ws/v1/conversations/{conversation_id}",
        {},
    )
    ws.connect()
    assert ws.sock is not None
    ws.sock.settimeout(5)
    try:
        snapshot = ws.recv_json()
        assert snapshot["event_type"] == "session.snapshot"

        patched = live_process_server.client.patch(
            f"/api/v1/conversations/{conversation_id}/guardrails",
            json={"log_level": "debug"},
        )
        assert patched.status_code == 200

        submit_user_message(
            live_process_server.client,
            conversation_id,
            client_message_id="phase5-ws-user",
            content_markdown="@A 触发公共监控事件",
            mentions=["agent-a"],
        )

        events: list[dict[str, Any]] = []
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                event = ws.recv_json()
            except TimeoutError:
                continue
            if event:
                events.append(event)
            event_types = {item.get("event_type") for item in events}
            if {
                "guardrails.changed",
                "agent.state_changed",
                "agent.run_updated",
                "message.committed",
            }.issubset(event_types):
                break

        event_types = {item["event_type"] for item in events}
        assert "guardrails.changed" in event_types
        assert "agent.state_changed" in event_types
        assert "agent.run_updated" in event_types
        assert "message.committed" in event_types
        assert any(
            item["event_type"] == "guardrails.changed"
            and item["payload"]["changed_fields"]["log_level"]["to"] == "debug"
            for item in events
        )
        assert any(
            item["event_type"] == "agent.run_updated"
            and item["payload"]["status"] in {"RUNNING", "COMMITTED"}
            for item in events
        )
    finally:
        ws.close()


def test_phase5_detail_exposes_runtime_authority_for_transient_state_reconciliation(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_phase4_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@A",
                "delay_ms": 200,
                "output_json": {"should_reply": True, "reply_key": "authority"},
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"authority\"",
                "output_json": {
                    "content_markdown": "A authority done",
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
        client_message_id="phase5-authority-user",
        content_markdown="@A 请开始但暂时不要结束",
        mentions=["agent-a"],
    )

    def _agent_state() -> dict[str, Any] | None:
        detail = live_process_server.client.get(f"/api/v1/conversations/{conversation_id}")
        assert detail.status_code == 200
        payload = cast(dict[str, Any], detail.json())
        runtime_authority = cast(dict[str, Any], payload["runtime_authority"])
        assert isinstance(runtime_authority["latest_reliable_seq"], int)
        return cast(dict[str, Any] | None, runtime_authority["agents"].get("agent-a"))

    wait_until(
        lambda: (_agent_state() or {}).get("typing_status") == "active"
    )
    active_state = _agent_state()
    assert active_state is not None
    assert active_state["worker_state"] in {"DECIDING", "ACTING", "SUBMITTING"}
    assert active_state["typing_run_id"] is not None

    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)
    wait_until(lambda: (_agent_state() or {}).get("typing_status") == "idle")
    idle_state = _agent_state()
    assert idle_state is not None
    assert idle_state["worker_state"] == "LISTENING"
    assert idle_state["typing_run_id"] is None


def test_stage7_post_returns_before_public_ws_dispatch_completes(
    live_process_server: LiveServer,
    monkeypatch: Any,
) -> None:
    conversation_id = _phase5_conversation(live_process_server)
    transcript = archive_root(live_process_server, conversation_id) / "transcript.sqlite"
    ws_url = live_process_server.base_url.replace("http://", "ws://")
    ws = RawWebSocketClient(
        f"{ws_url}/ws/v1/conversations/{conversation_id}",
        {},
    )
    ws.connect()
    assert ws.sock is not None
    ws.sock.settimeout(5)
    try:
        snapshot = ws.recv_json()
        assert snapshot["event_type"] == "session.snapshot"

        original_publish = SOCKET_MANAGER.publish

        def delayed_publish(conversation_id_arg: str, event: dict[str, Any]) -> None:
            time.sleep(0.2)
            original_publish(conversation_id_arg, event)

        monkeypatch.setattr(SOCKET_MANAGER, "publish", delayed_publish)
        started = time.perf_counter()
        response = live_process_server.client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={
                "client_message_id": "phase5-post-latency",
                "content_markdown": "plain post latency probe",
                "mentions": [],
                "responds_to": [],
                "primary_reply_to": None,
            },
        )
        post_ms = (time.perf_counter() - started) * 1000
        assert response.status_code == 201
        row = _fetch_transcript_row(
            transcript,
            """
            SELECT dispatch_attempts, dispatched_at
            FROM outbox_events
            WHERE event_type = 'message.committed'
            ORDER BY rowid DESC
            LIMIT 1
            """,
        )
        assert int(row["dispatch_attempts"]) == 0
        assert row["dispatched_at"] is None

        visible_started = time.perf_counter()
        deadline = time.time() + 5
        event: dict[str, Any] | None = None
        while time.time() < deadline:
            candidate = ws.recv_json()
            if candidate["event_type"] == "message.committed":
                event = candidate
                break
        assert event is not None
        ws_ms = (time.perf_counter() - visible_started) * 1000
        assert post_ms < 200
        assert ws_ms >= 150
    finally:
        ws.close()
