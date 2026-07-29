from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from conftest import LiveServer
from test_phase3_worker_processes import (
    agent_messages,
    configure_runtime,
    fetch_rows,
    submit_user_message,
    wait_until,
    worker_snapshot,
)


def configure_phase4_runtime(
    server: LiveServer,
    *,
    script: list[dict[str, Any]],
    memory_module_id: str = "memory.graph-overlay",
    context_module_id: str = "context.simple",
    context_config: dict[str, Any] | None = None,
    worker_restart_limit: int | None = None,
) -> str:
    conversation_id = configure_runtime(
        server,
        script=script,
        worker_restart_limit=worker_restart_limit,
    )
    detail = server.client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    conversation = detail.json()["conversation"]
    draft_profile = conversation["draft_profile"]
    draft_profile["metadata"]["phase"] = 4
    draft_profile["metadata"]["agent_runtime_enabled"] = True
    draft_profile["modules"]["memory"] = {
        "module_id": memory_module_id,
        "config": {},
    }
    draft_profile["modules"]["context_assembly"] = {
        "module_id": context_module_id,
        "config": context_config or {},
    }
    assert server.client.post(f"/api/v1/conversations/{conversation_id}/end").status_code == 200

    recreated = server.client.post(
        "/api/v1/conversations",
        json={"title": "Phase 4 Runtime", "draft_profile": draft_profile},
    )
    assert recreated.status_code == 201
    new_conversation_id = cast(str, recreated.json()["id"])
    if worker_restart_limit is not None:
        patched = server.client.patch(
            f"/api/v1/conversations/{new_conversation_id}/guardrails",
            json={"worker_restart_limit": worker_restart_limit},
        )
        assert patched.status_code == 200
    validate = server.client.post(f"/api/v1/conversations/{new_conversation_id}/validate")
    assert validate.status_code == 200
    started = server.client.post(f"/api/v1/conversations/{new_conversation_id}/start")
    assert started.status_code == 200
    return new_conversation_id


def archive_root(server: LiveServer, conversation_id: str) -> Path:
    detail = server.client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    return Path(detail.json()["conversation"]["archive_dir"])


def state_dir(server: LiveServer, conversation_id: str, agent_id: str) -> Path:
    return archive_root(server, conversation_id) / "module-state" / agent_id


def read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def read_run_trace(conversation_id: str, agent_id: str) -> dict[str, Any]:
    row = fetch_rows(
        conversation_id,
        f"""
        SELECT decision_json, draft_message_json
        FROM agent_runs
        WHERE agent_id = '{agent_id}' AND status = 'COMMITTED'
        ORDER BY started_at DESC
        LIMIT 1
        """,
    )[0]
    return {
        "decision": json.loads(row["decision_json"]),
        "draft": json.loads(row["draft_message_json"]),
    }


def latest_context_bundle(dir_path: Path) -> dict[str, Any]:
    files = sorted((dir_path / "context-bundles").glob("ctx-*.json"))
    assert files
    return read_json(files[-1])


def test_phase4_default_graph_memory_and_bundle_revision_trace(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_phase4_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@A 第一轮",
                "output_json": {
                    "should_reply": True,
                    "reply_key": "first",
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "upsert_node",
                                "node": {
                                    "memory_node_id": "belief-1",
                                    "type": "Belief",
                                    "content": "第一轮记忆",
                                    "confidence": 0.8,
                                    "salience": 0.9,
                                    "status": "open",
                                    "source_refs": ["first-user"],
                                },
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"first\"",
                "output_json": {
                    "content_markdown": "A 第一轮已记录",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "set_overlay",
                                "projection_strategy_id": "projection.segment-chain",
                                "projection_revision": 1,
                                "projection_object_ref": "segment:latest",
                                "salience": 0.7,
                                "unresolved": True,
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@A 第二轮",
                "output_json": {
                    "should_reply": True,
                    "reply_key": "second",
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "upsert_node",
                                "node": {
                                    "memory_node_id": "belief-2",
                                    "type": "Belief",
                                    "content": "第二轮记忆",
                                    "confidence": 0.6,
                                    "salience": 0.5,
                                    "status": "open",
                                    "source_refs": ["second-user"],
                                },
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"second\"",
                "output_json": {
                    "content_markdown": "A 第二轮已记录",
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
        client_message_id="first-user",
        content_markdown="@A 第一轮",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)

    agent_a_dir = state_dir(live_process_server, conversation_id, "agent-a")
    current_after_first = read_json(agent_a_dir / "memory" / "current.json")
    mem1_snapshot = read_json(agent_a_dir / "memory" / "revisions" / "mem-1.json")
    ctx1_snapshot = read_json(agent_a_dir / "context-bundles" / "ctx-1.json")
    assert current_after_first["revision"] == "mem-1"
    assert any(node["content"] == "第一轮记忆" for node in current_after_first["payload"]["nodes"])
    assert current_after_first["payload"]["overlay"]
    first_trace = read_run_trace(conversation_id, "agent-a")
    assert first_trace["decision"]["bundle_revision"] == "ctx-1"
    assert first_trace["draft"]["bundle_revision"] == "ctx-2"
    assert first_trace["decision"]["memory_revision_before"] == "mem-0"
    assert first_trace["draft"]["memory_revision_after"] == "mem-1"
    assert ctx1_snapshot["memory_revision"] == "mem-0"

    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="second-user",
        content_markdown="@A 第二轮",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 2)
    current_after_second = read_json(agent_a_dir / "memory" / "current.json")
    assert current_after_second["revision"] == "mem-2"
    assert any(node["content"] == "第二轮记忆" for node in current_after_second["payload"]["nodes"])
    assert read_json(agent_a_dir / "memory" / "revisions" / "mem-1.json") == mem1_snapshot
    assert read_json(agent_a_dir / "context-bundles" / "ctx-1.json") == ctx1_snapshot


def test_phase4_low_complexity_recent_window_no_memory(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_phase4_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "output_json": {
                    "should_reply": True,
                    "memory_delta": {
                        "operations": [{"op": "upsert_node", "node": {"content": "ignored-a"}}]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "output_json": {
                    "content_markdown": "A 简化策略",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
        memory_module_id="memory.no-memory",
        context_module_id="context.recent-window",
        context_config={"recent_public_message_count": 3},
    )
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="simple-user",
        content_markdown="@A 简单模式",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)

    agent_a_dir = state_dir(live_process_server, conversation_id, "agent-a")
    current_memory = read_json(agent_a_dir / "memory" / "current.json")
    bundle = latest_context_bundle(agent_a_dir)
    assert current_memory["revision"] == "mem-0"
    assert sorted(path.name for path in (agent_a_dir / "memory" / "revisions").glob("*.json")) == [
        "mem-0.json"
    ]
    assert bundle["selected_private_refs"] == []
    assert not any(item.startswith("[memory]") for item in bundle["rendered_items"])


def test_phase4_dual_agent_private_memory_isolation(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_phase4_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@all 私有隔离",
                "output_json": {
                    "should_reply": True,
                    "reply_key": "a",
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "upsert_node",
                                "node": {
                                    "memory_node_id": "a-private",
                                    "type": "Belief",
                                    "content": "A 私有记忆",
                                },
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"a\"",
                "output_json": {
                    "content_markdown": "A 私有完成",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
            {
                "purpose": "decision",
                "agent_id": "agent-b",
                "match_contains": "@all 私有隔离",
                "output_json": {
                    "should_reply": True,
                    "reply_key": "b",
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "upsert_node",
                                "node": {
                                    "memory_node_id": "b-private",
                                    "type": "Belief",
                                    "content": "B 私有记忆",
                                },
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-b",
                "match_contains": "\"reply_key\": \"b\"",
                "output_json": {
                    "content_markdown": "B 私有完成",
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
        client_message_id="isolation-user",
        content_markdown="@all 私有隔离",
        mentions=["agent-a", "agent-b"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 2)

    agent_a_memory = read_json(
        state_dir(live_process_server, conversation_id, "agent-a")
        / "memory"
        / "current.json"
    )
    agent_b_memory = read_json(
        state_dir(live_process_server, conversation_id, "agent-b")
        / "memory"
        / "current.json"
    )
    assert "A 私有记忆" in json.dumps(agent_a_memory, ensure_ascii=False)
    assert "B 私有记忆" not in json.dumps(agent_a_memory, ensure_ascii=False)
    assert "B 私有记忆" in json.dumps(agent_b_memory, ensure_ascii=False)
    assert "A 私有记忆" not in json.dumps(agent_b_memory, ensure_ascii=False)


def test_phase4_restart_discards_staged_memory_delta(
    live_process_server: LiveServer,
) -> None:
    conversation_id = configure_phase4_runtime(
        live_process_server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@A 崩溃",
                "output_json": {
                    "should_reply": True,
                    "reply_key": "crash",
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "upsert_node",
                                "node": {
                                    "memory_node_id": "crash-node",
                                    "type": "Belief",
                                    "content": "崩溃前暂存",
                                },
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"crash\"",
                "fail_code": "WORKER_CRASHED",
            },
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@A 重试",
                "output_json": {
                    "should_reply": True,
                    "reply_key": "retry",
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "upsert_node",
                                "node": {
                                    "memory_node_id": "retry-node",
                                    "type": "Belief",
                                    "content": "恢复后提交",
                                },
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"retry\"",
                "output_json": {
                    "content_markdown": "A 恢复成功",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
        worker_restart_limit=1,
    )
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="crash-user",
        content_markdown="@A 崩溃",
        mentions=["agent-a"],
    )
    wait_until(
        lambda: worker_snapshot(live_process_server, conversation_id)["agent-a"][
            "restart_count"
        ]
        == 1
    )
    wait_until(lambda: not agent_messages(live_process_server.client, conversation_id))

    agent_a_dir = state_dir(live_process_server, conversation_id, "agent-a")
    current_after_crash = read_json(agent_a_dir / "memory" / "current.json")
    assert current_after_crash["revision"] == "mem-0"
    assert "崩溃前暂存" not in json.dumps(current_after_crash, ensure_ascii=False)

    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="retry-user",
        content_markdown="@A 重试",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)
    current_after_retry = read_json(agent_a_dir / "memory" / "current.json")
    assert current_after_retry["revision"] == "mem-1"
    assert "恢复后提交" in json.dumps(current_after_retry, ensure_ascii=False)
    assert "崩溃前暂存" not in json.dumps(current_after_retry, ensure_ascii=False)
