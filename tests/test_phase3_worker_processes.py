from __future__ import annotations

import os
import signal
import sqlite3
import time
from pathlib import Path
from typing import Any, cast

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
) -> tuple[str, dict[str, Any]]:
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
    conversation = cast(dict[str, Any], created.json())
    conversation_id = cast(str, conversation["id"])
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
    return conversation_id, cast(dict[str, Any], started.json())


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
    supervisor = cast(Any, server.app.state.worker_supervisor)
    record, profile = load_record_and_profile(conversation_id)
    snapshot = supervisor.runtime_snapshot(record, profile=profile)
    return {item["agent_id"]: item for item in snapshot["workers"]}


def auth_headers(server: LiveServer, conversation_id: str, agent_id: str) -> dict[str, str]:
    supervisor = cast(Any, server.app.state.worker_supervisor)
    handle = supervisor._handles[conversation_id][agent_id]
    return {
        "Authorization": f"Bearer {handle.token}",
        "X-Agent-ID": agent_id,
        "X-Profile-Hash": handle.profile_hash,
    }


def test_real_worker_auth_isolation_and_revocation(live_process_server: LiveServer) -> None:
    conversation_id, _ = configure_runtime(live_process_server, script=[])
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
    conversation_id, _ = configure_runtime(
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


def test_real_workers_keep_contiguous_sequence_after_cross_process_retry(
    live_process_server: LiveServer,
) -> None:
    conversation_id, _ = configure_runtime(
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
                    "content_markdown": "A 先回应",
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
                    "content_markdown": "B 随后补位",
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
        client_message_id="all-user",
        content_markdown="@all 两位都来",
        mentions=["agent-a", "agent-b"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 2)

    messages = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/messages"
    )
    assert messages.status_code == 200
    items = cast(list[dict[str, Any]], messages.json()["items"])
    assert [item["conversation_seq"] for item in items] == [1, 2, 3]
    assert [item["sender_id"] for item in items[1:]] in (
        ["agent-a", "agent-b"],
        ["agent-b", "agent-a"],
    )

    def run_statuses() -> list[str]:
        return [
            row["status"]
            for row in fetch_rows(
                conversation_id,
                "SELECT status FROM agent_runs ORDER BY started_at, agent_id",
            )
        ]

    wait_until(lambda: run_statuses() == ["COMMITTED", "COMMITTED"])
    runs = fetch_rows(
        conversation_id,
        "SELECT status FROM agent_runs ORDER BY started_at, agent_id",
    )
    assert [row["status"] for row in runs] == ["COMMITTED", "COMMITTED"]
