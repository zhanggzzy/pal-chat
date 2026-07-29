from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, cast

from conftest import LiveServer
from test_phase3_worker_processes import agent_messages, submit_user_message, wait_until
from test_phase4_memory_context import archive_root, configure_phase4_runtime


def _phase6_conversation(server: LiveServer) -> str:
    return configure_phase4_runtime(
        server,
        script=[
            {
                "purpose": "decision",
                "agent_id": "agent-a",
                "match_contains": "@A 阶段 6",
                "output_json": {
                    "should_reply": True,
                    "reply_key": "phase6",
                    "memory_delta": {
                        "operations": [
                            {
                                "op": "upsert_node",
                                "node": {
                                    "memory_node_id": "phase6-node",
                                    "type": "Belief",
                                    "content": "阶段 6 记忆",
                                    "confidence": 0.7,
                                    "salience": 0.8,
                                    "status": "open",
                                    "source_refs": ["phase6-user"],
                                },
                            }
                        ]
                    },
                },
            },
            {
                "purpose": "action",
                "agent_id": "agent-a",
                "match_contains": "\"reply_key\": \"phase6\"",
                "output_json": {
                    "content_markdown": "A 已完成阶段 6 响应",
                    "mentions": [],
                    "primary_reply_to": None,
                    "responds_to": [],
                },
            },
        ],
    )


def _end_conversation(server: LiveServer, conversation_id: str) -> None:
    response = server.client.post(f"/api/v1/conversations/{conversation_id}/end")
    assert response.status_code == 200


def _set_sensitive_metadata(server: LiveServer, conversation_id: str) -> None:
    patched = server.client.patch(
        f"/api/v1/conversations/{conversation_id}/catalog-metadata",
        json={"metadata": {"api_token": "secret-123", "viewer_note": "phase6"}},
    )
    assert patched.status_code == 200


def test_h14_history_index_and_raw_fallback_stay_read_only(
    live_process_server: LiveServer,
) -> None:
    conversation_id = _phase6_conversation(live_process_server)
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="phase6-user",
        content_markdown="@A 阶段 6",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)

    metrics = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/metrics/automatic"
    )
    assert metrics.status_code == 200
    metric_payload = cast(dict[str, Any], metrics.json())
    assert metric_payload["metrics"]["message_count"] == 2
    assert metric_payload["metrics"]["committed_run_count"] == 1

    manual = live_process_server.client.put(
        f"/api/v1/conversations/{conversation_id}/manual-score",
        json={
            "rubric_version": "manual-score-v1",
            "scores": [
                {"criterion": "clarity", "score": 4, "note": "回复完整"},
                {"criterion": "safety", "score": 5, "note": "无额外暴露"},
            ],
            "overall_note": "人工检查通过",
        },
    )
    assert manual.status_code == 200
    assert manual.json()["overall_score"] == 4.5

    _set_sensitive_metadata(live_process_server, conversation_id)
    _end_conversation(live_process_server, conversation_id)

    detail = live_process_server.client.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    manifest_path = Path(detail.json()["manifest"]["manifest_path"])
    manifest = cast(dict[str, Any], json.loads(manifest_path.read_text(encoding="utf-8")))
    manifest["draft_profile"]["modules"]["model_adapter"]["module_id"] = "model.legacy-unknown"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    history_index = live_process_server.client.get("/api/v1/history")
    assert history_index.status_code == 200
    history_items = cast(list[dict[str, Any]], history_index.json()["items"])
    entry = next(item for item in history_items if item["conversation_id"] == conversation_id)
    assert entry["adapter_module_id"] == "model.legacy-unknown"
    assert entry["adapter_known"] is False

    before_handles = dict(live_process_server.app.state.worker_supervisor._handles)
    history_detail = live_process_server.client.get(f"/api/v1/history/{conversation_id}")
    assert history_detail.status_code == 200
    payload = cast(dict[str, Any], history_detail.json())
    assert payload["entry"]["conversation_id"] == conversation_id
    assert payload["raw_manifest"]["draft_profile"]["modules"]["model_adapter"]["module_id"] == (
        "model.legacy-unknown"
    )
    assert payload["raw_manifest"]["catalog_metadata"]["api_token"] == "[REDACTED]"
    assert len(payload["messages"]) == 2
    assert payload["messages"][1]["content_markdown"] == "A 已完成阶段 6 响应"
    assert payload["cp_revisions"][-1]["projection_revision"] >= 1
    assert dict(live_process_server.app.state.worker_supervisor._handles) == before_handles

    after_detail = live_process_server.client.get(f"/api/v1/conversations/{conversation_id}")
    assert after_detail.status_code == 200
    assert after_detail.json()["conversation"]["status"] == "ended"


def test_h15_analysis_export_zip_is_versioned_redacted_and_downloadable(
    live_process_server: LiveServer,
) -> None:
    conversation_id = _phase6_conversation(live_process_server)
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="phase6-export-user",
        content_markdown="@A 阶段 6",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)
    _set_sensitive_metadata(live_process_server, conversation_id)
    _end_conversation(live_process_server, conversation_id)

    created = live_process_server.client.post(
        f"/api/v1/conversations/{conversation_id}/analysis-exports"
    )
    assert created.status_code == 202
    job = cast(dict[str, Any], created.json())
    job_id = str(job["job_id"])

    def _job_ready() -> bool:
        response = live_process_server.client.get(
            f"/api/v1/conversations/{conversation_id}/analysis-exports/{job_id}"
        )
        assert response.status_code == 200
        current = cast(dict[str, Any], response.json())
        assert current["status"] in {"queued", "running", "ready"}
        return bool(current["status"] == "ready")

    wait_until(_job_ready)
    ready = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/analysis-exports/{job_id}"
    )
    assert ready.status_code == 200
    ready_payload = cast(dict[str, Any], ready.json())
    assert ready_payload["download_url"] is not None

    downloaded = live_process_server.client.get(cast(str, ready_payload["download_url"]))
    assert downloaded.status_code == 200
    zip_bytes = downloaded.content
    assert b"secret-123" not in zip_bytes

    export_path = (
        archive_root(live_process_server, conversation_id)
        / "exports"
        / f"analysis-{job_id}.zip"
    )
    assert export_path.exists()

    with zipfile.ZipFile(export_path) as archive:
        names = sorted(archive.namelist())
        assert names == sorted(
            [
                "analysis/automatic-metrics.json",
                "analysis/manual-score.json",
                "manifest.json",
                "manifest/raw.json",
                "public/cp-revisions.json",
                "public/history.json",
                "public/logs.json",
                "public/messages.json",
            ]
        )
        manifest_payload = cast(
            dict[str, Any],
            json.loads(archive.read("manifest.json").decode("utf-8")),
        )
        assert manifest_payload["analysis_bundle_version"] == 1
        assert manifest_payload["conversation_id"] == conversation_id
        file_entries = cast(list[dict[str, Any]], manifest_payload["files"])
        for entry in file_entries:
            content = archive.read(str(entry["path"]))
            assert int(entry["size_bytes"]) == len(content)
            assert str(entry["sha256"]) == hashlib.sha256(content).hexdigest()
        raw_manifest = cast(
            dict[str, Any],
            json.loads(archive.read("manifest/raw.json").decode("utf-8")),
        )
        assert raw_manifest["catalog_metadata"]["api_token"] == "[REDACTED]"
        history_payload = cast(
            dict[str, Any],
            json.loads(archive.read("public/history.json").decode("utf-8")),
        )
        assert history_payload["conversation"]["status"] == "ended"

    after_detail = live_process_server.client.get(f"/api/v1/conversations/{conversation_id}")
    assert after_detail.status_code == 200
    assert after_detail.json()["conversation"]["status"] == "ended"
