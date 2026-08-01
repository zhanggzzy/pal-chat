from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path
from typing import Any, cast

from conftest import LiveServer, live_server_context
from pal_chat_server.sequence_runtime import SOCKET_MANAGER
from test_phase3_worker_processes import (
    agent_messages,
    default_profile_payload,
    submit_user_message,
    wait_until,
)
from test_phase4_memory_context import archive_root, configure_phase4_runtime


def _phase6_conversation(server: LiveServer) -> str:
    return configure_phase4_runtime(
        server,
        script=_phase6_script(),
    )


def _phase6_script() -> list[dict[str, Any]]:
    return [
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
    ]


def _phase6_costed_conversation(server: LiveServer) -> str:
    profile = default_profile_payload(server.client)
    profile["metadata"]["phase"] = 4
    profile["metadata"]["agent_runtime_enabled"] = True
    profile["modules"]["model_adapter"] = {
        "module_id": "model.scripted",
        "config": {"script": _phase6_script()},
    }
    profile["modules"]["memory"] = {"module_id": "memory.graph-overlay", "config": {}}
    profile["modules"]["context_assembly"] = {
        "module_id": "context.simple",
        "config": {},
    }
    profile["agent_a"]["model"]["provider"] = "openai"
    profile["agent_a"]["model"]["model"] = "gpt-4o-mini"
    created = server.client.post(
        "/api/v1/conversations",
        json={"title": "Phase 6 Costed Runtime", "draft_profile": profile},
    )
    assert created.status_code == 201
    conversation_id = cast(str, created.json()["id"])
    validate = server.client.post(f"/api/v1/conversations/{conversation_id}/validate")
    assert validate.status_code == 200
    started = server.client.post(f"/api/v1/conversations/{conversation_id}/start")
    assert started.status_code == 200
    return conversation_id


def _end_conversation(server: LiveServer, conversation_id: str) -> None:
    response = server.client.post(f"/api/v1/conversations/{conversation_id}/end")
    assert response.status_code == 200


def _set_sensitive_metadata(server: LiveServer, conversation_id: str) -> None:
    patched = server.client.patch(
        f"/api/v1/conversations/{conversation_id}/catalog-metadata",
        json={
            "metadata": {
                "api_token": "secret-123",
                "viewer_note": "phase6",
                "max_total_tokens": 250000,
            }
        },
    )
    assert patched.status_code == 200


def _manual_score_path(server: LiveServer, conversation_id: str) -> Path:
    return archive_root(server, conversation_id) / "analysis" / "manual-score.v1.json"


def _job_json_path(server: LiveServer, conversation_id: str, job_id: str) -> Path:
    return (
        archive_root(server, conversation_id)
        / "exports"
        / "analysis-jobs"
        / f"{job_id}.json"
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


def _observation_event_ids(root: Path) -> list[str]:
    observations_path = root / "observations.ndjson"
    if not observations_path.exists():
        return []
    event_ids: list[str] = []
    for line in observations_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = cast(dict[str, Any], json.loads(line))
        nested = cast(dict[str, Any], payload.get("payload") or {})
        event_id = nested.get("event_id")
        if isinstance(event_id, str):
            event_ids.append(event_id)
    return event_ids


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
    assert metric_payload["metrics"]["total_attempt_tokens"] > 0
    assert metric_payload["metrics"]["total_cost_usd"] == 0.0

    manual_score_path = _manual_score_path(live_process_server, conversation_id)
    assert not manual_score_path.exists()
    for score in (0, 99):
        rejected = live_process_server.client.put(
            f"/api/v1/conversations/{conversation_id}/manual-score",
            json={
                "rubric_version": "manual-score-v1",
                "scores": [{"criterion": "clarity", "score": score, "note": "invalid"}],
                "overall_note": "should fail",
            },
        )
        assert rejected.status_code == 422
        assert not manual_score_path.exists()

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
    assert payload["raw_manifest"]["catalog_metadata"]["max_total_tokens"] == 250000
    assert len(payload["messages"]) == 2
    assert payload["messages"][1]["content_markdown"] == "A 已完成阶段 6 响应"
    assert payload["cp_revisions"][-1]["projection_revision"] >= 1
    assert dict(live_process_server.app.state.worker_supervisor._handles) == before_handles

    after_detail = live_process_server.client.get(f"/api/v1/conversations/{conversation_id}")
    assert after_detail.status_code == 200
    assert after_detail.json()["conversation"]["status"] == "ended"


def test_phase6_automatic_metrics_accumulate_attempt_costs(
    live_process_server: LiveServer,
) -> None:
    conversation_id = _phase6_costed_conversation(live_process_server)
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="phase6-cost-user",
        content_markdown="@A 阶段 6",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)

    metrics = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/metrics/automatic"
    )
    assert metrics.status_code == 200
    metric_payload = cast(dict[str, Any], metrics.json())

    costs = live_process_server.client.get(
        f"/api/v1/conversations/{conversation_id}/metrics/cost-breakdown"
    )
    assert costs.status_code == 200
    cost_payload = cast(dict[str, Any], costs.json())

    assert metric_payload["metrics"]["total_attempt_tokens"] == cost_payload["total_tokens"]
    assert metric_payload["metrics"]["total_cost_usd"] == cost_payload["total_cost_usd"]
    assert metric_payload["metrics"]["total_cost_usd"] > 0


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
    expected_zip_bytes = export_path.read_bytes()
    job_json_path = _job_json_path(live_process_server, conversation_id, job_id)
    job_payload = cast(dict[str, Any], json.loads(job_json_path.read_text(encoding="utf-8")))
    for tampered_path in (
        "manifest.json",
        "../manifest.json",
        str(archive_root(live_process_server, conversation_id) / "manifest.json"),
        str(job_json_path),
    ):
        job_payload["download_path"] = tampered_path
        job_json_path.write_text(
            json.dumps(job_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tampered = live_process_server.client.get(cast(str, ready_payload["download_url"]))
        assert tampered.status_code == 200
        assert tampered.content == expected_zip_bytes
        assert tampered.content[:1] != b"{"

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
        assert raw_manifest["catalog_metadata"]["max_total_tokens"] == 250000
        history_payload = cast(
            dict[str, Any],
            json.loads(archive.read("public/history.json").decode("utf-8")),
        )
        assert history_payload["conversation"]["status"] == "ended"

    export_path.write_text("{not-a-zip}\n", encoding="utf-8")
    invalid_download = live_process_server.client.get(cast(str, ready_payload["download_url"]))
    assert invalid_download.status_code == 409

    after_detail = live_process_server.client.get(f"/api/v1/conversations/{conversation_id}")
    assert after_detail.status_code == 200
    assert after_detail.json()["conversation"]["status"] == "ended"


def test_stage7_restart_recovers_pending_outbox_and_analysis_export(
    data_dir: Path,
) -> None:
    with live_server_context(data_dir) as server:
        conversation_id = _phase6_conversation(server)
        submit_user_message(
            server.client,
            conversation_id,
            client_message_id="phase7-restart-user",
            content_markdown="@A 阶段 6",
            mentions=["agent-a"],
        )
        wait_until(lambda: len(agent_messages(server.client, conversation_id)) == 1)
        _set_sensitive_metadata(server, conversation_id)
        _end_conversation(server, conversation_id)

        root = archive_root(server, conversation_id)
        transcript = root / "transcript.sqlite"
        connection = sqlite3.connect(transcript)
        try:
            connection.execute(
                """
                INSERT INTO outbox_events(
                  event_id, event_type, conversation_seq, payload_json, status,
                  dispatch_attempts, dispatched_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "evt-stage7-recovery",
                    "agent.typing_stopped",
                    None,
                    json.dumps(
                        {
                            "agent_id": "agent-a",
                            "run_id": "run-recovery",
                            "reason": "restart-recovery",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "pending",
                    0,
                    None,
                    "2026-07-31T15:30:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        job_id = "job-restart-recovery"
        job_path = root / "exports" / "analysis-jobs" / f"{job_id}.json"
        job_path.parent.mkdir(parents=True, exist_ok=True)
        job_path.write_text(
            json.dumps(
                {
                    "job_id": job_id,
                    "conversation_id": conversation_id,
                    "status": "running",
                    "created_at": "2026-07-31T15:30:01+00:00",
                    "updated_at": "2026-07-31T15:30:01+00:00",
                    "download_path": None,
                    "error": None,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    with live_server_context(data_dir) as server:
        transcript = archive_root(server, conversation_id) / "transcript.sqlite"
        root = archive_root(server, conversation_id)

        def outbox_recovered() -> bool:
            row = _fetch_transcript_row(
                transcript,
                """
                SELECT dispatch_attempts, dispatched_at
                FROM outbox_events
                WHERE event_id = ?
                """,
                ("evt-stage7-recovery",),
            )
            return int(row["dispatch_attempts"]) == 1 and row["dispatched_at"] is not None

        wait_until(outbox_recovered)

        def export_recovered() -> bool:
            response = server.client.get(
                f"/api/v1/conversations/{conversation_id}/analysis-exports/{job_id}"
            )
            assert response.status_code == 200
            return cast(str, response.json()["status"]) == "ready"

        wait_until(export_recovered)
        wait_until(
            lambda: not cast(list[str], server.app.state.outbox_dispatcher.snapshot()["pending"])
            and not cast(list[str], server.app.state.outbox_dispatcher.snapshot()["active"])
        )
        ready = server.client.get(
            f"/api/v1/conversations/{conversation_id}/analysis-exports/{job_id}"
        )
        assert ready.status_code == 200
        download_url = cast(str, ready.json()["download_url"])
        downloaded = server.client.get(download_url)
        assert downloaded.status_code == 200
        export_path = archive_root(server, conversation_id) / "exports" / f"analysis-{job_id}.zip"
        assert zipfile.is_zipfile(Path(export_path))
        assert _observation_event_ids(root).count("evt-stage7-recovery") == 1

    with live_server_context(data_dir) as server:
        transcript = archive_root(server, conversation_id) / "transcript.sqlite"
        root = archive_root(server, conversation_id)
        row = _fetch_transcript_row(
            transcript,
            """
            SELECT dispatch_attempts, dispatched_at
            FROM outbox_events
            WHERE event_id = ?
            """,
            ("evt-stage7-recovery",),
        )
        assert int(row["dispatch_attempts"]) == 1
        assert row["dispatched_at"] is not None
        ready = server.client.get(
            f"/api/v1/conversations/{conversation_id}/analysis-exports/{job_id}"
        )
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        assert _observation_event_ids(root).count("evt-stage7-recovery") == 1
        snapshot = cast(dict[str, Any], server.app.state.outbox_dispatcher.snapshot())
        assert snapshot["pending"] == []
        assert snapshot["active"] == []


def test_stage7_dispatcher_retries_failed_outbox_delivery(
    live_process_server: LiveServer,
    monkeypatch: Any,
) -> None:
    conversation_id = _phase6_conversation(live_process_server)
    submit_user_message(
        live_process_server.client,
        conversation_id,
        client_message_id="phase7-dispatcher-retry-user",
        content_markdown="@A 阶段 6",
        mentions=["agent-a"],
    )
    wait_until(lambda: len(agent_messages(live_process_server.client, conversation_id)) == 1)
    transcript = archive_root(live_process_server, conversation_id) / "transcript.sqlite"
    call_count = {"value": 0}
    original_publish = SOCKET_MANAGER.publish

    def flaky_publish(conversation_id_arg: str, event: dict[str, Any]) -> None:
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("inject one-shot outbox failure")
        original_publish(conversation_id_arg, event)

    monkeypatch.setattr(SOCKET_MANAGER, "publish", flaky_publish)

    connection = sqlite3.connect(transcript)
    try:
        connection.execute(
            """
            INSERT INTO outbox_events(
              event_id, event_type, conversation_seq, payload_json, status,
              dispatch_attempts, dispatched_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt-stage7-dispatcher-retry",
                "agent.typing_stopped",
                None,
                json.dumps(
                    {
                        "agent_id": "agent-a",
                        "run_id": "run-dispatch-retry",
                        "reason": "retry-probe",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "pending",
                0,
                None,
                "2026-07-31T18:40:00+00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    live_process_server.app.state.outbox_dispatcher.enqueue(conversation_id)

    def outbox_recovered() -> bool:
        row = _fetch_transcript_row(
            transcript,
            """
            SELECT dispatch_attempts, dispatched_at
            FROM outbox_events
            WHERE event_id = ?
            """,
            ("evt-stage7-dispatcher-retry",),
        )
        return int(row["dispatch_attempts"]) == 1 and row["dispatched_at"] is not None

    wait_until(outbox_recovered)
    assert call_count["value"] >= 2


def test_stage7_dispatcher_shutdown_converges(data_dir: Path) -> None:
    dispatcher = None
    with live_server_context(data_dir) as server:
        dispatcher = server.app.state.outbox_dispatcher
        assert dispatcher._thread is not None
        assert dispatcher._thread.is_alive()
        snapshot = cast(dict[str, Any], dispatcher.snapshot())
        assert snapshot["pending"] == []
        assert snapshot["active"] == []
    assert dispatcher is not None
    assert dispatcher._thread is not None
    assert not dispatcher._thread.is_alive()
