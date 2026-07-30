from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pal_chat_server.archive import scan_manifests
from pal_chat_server.config import Settings
from pal_chat_server.errors import AppError
from pal_chat_server.models import ConversationRecord
from pal_chat_server.monitoring import attempts_for_run
from pal_chat_server.registry import MODULE_REGISTRY
from pal_chat_server.schemas import ExperimentProfile
from pal_chat_server.sequence_runtime import utc_now

AUTOMATIC_METRICS_FILENAME = "automatic-metrics.v1.json"
MANUAL_SCORE_FILENAME = "manual-score.v1.json"
EXPORT_JOBS_DIRNAME = "analysis-jobs"
EXPORT_MANIFEST_VERSION = 1

_EXPORT_LOCK = threading.Lock()


def _require_archive_root(conversation: ConversationRecord) -> Path:
    if conversation.archive_dir is None:
        raise AppError(
            code="archive_missing",
            status_code=409,
            message="Conversation archive is missing.",
        )
    return Path(conversation.archive_dir)


def _analysis_dir(root: Path) -> Path:
    path = root / "analysis"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _exports_dir(root: Path) -> Path:
    path = root / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _json_read(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _known_module_ids() -> set[str]:
    return {item.module_id for item in MODULE_REGISTRY}


def _manifest_for_conversation(settings: Settings, conversation_id: str) -> dict[str, Any]:
    for manifest in scan_manifests(settings):
        if str(manifest.get("conversation_id")) == conversation_id:
            return manifest
    raise AppError(
        code="history_not_found",
        status_code=404,
        message="History archive not found.",
    )


def _archive_root_from_manifest(manifest: dict[str, Any]) -> Path:
    archive_dir = manifest.get("archive_dir")
    if not archive_dir:
        raise AppError(
            code="archive_missing",
            status_code=409,
            message="History archive is missing its archive directory.",
        )
    return Path(str(archive_dir))


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _latency_ms(started_at: str | None, finished_at: str | None) -> int | None:
    start = _parse_time(started_at)
    end = _parse_time(finished_at)
    if start is None or end is None:
        return None
    return max(int((end - start).total_seconds() * 1000), 0)


def _sanitize_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("secret", "token", "password", "authorization"))


def redact_payload(value: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if _sanitize_key(str(key))
                else redact_payload(item, parent_key=str(key))
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item, parent_key=parent_key) for item in value]
    if isinstance(value, str) and parent_key is not None and _sanitize_key(parent_key):
        return "[REDACTED]"
    return value


def _transcript_path(root: Path) -> Path:
    return root / "transcript.sqlite"


def _transcript_rows(root: Path, query: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    path = _transcript_path(root)
    if not path.exists():
        return []
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(query, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _public_messages(root: Path) -> list[dict[str, Any]]:
    path = _transcript_path(root)
    if not path.exists():
        return []
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT *
            FROM messages
            ORDER BY conversation_seq
            """
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            mentions = [
                ref["member_id"]
                for ref in connection.execute(
                    """
                    SELECT member_id
                    FROM message_mentions
                    WHERE message_id = ?
                    ORDER BY member_id
                    """,
                    (row["message_id"],),
                ).fetchall()
            ]
            responds_to = [
                ref["responds_to_message_id"]
                for ref in connection.execute(
                    """
                    SELECT responds_to_message_id
                    FROM message_response_refs
                    WHERE message_id = ?
                    ORDER BY ordinal
                    """,
                    (row["message_id"],),
                ).fetchall()
            ]
            items.append(
                {
                    "message_id": row["message_id"],
                    "conversation_seq": int(row["conversation_seq"]),
                    "sender_kind": row["sender_kind"],
                    "sender_id": row["sender_id"],
                    "content_markdown": row["content_markdown"],
                    "mentions": mentions,
                    "primary_reply_to": row["primary_reply_to"],
                    "responds_to": responds_to,
                    "client_message_id": row["client_message_id"],
                    "causal_episode_id": row["causal_episode_id"],
                    "caused_by_message_id": row["caused_by_message_id"],
                    "agent_hop": int(row["agent_hop"]),
                    "committed_at": row["committed_at"],
                    "cp_revision": int(row["cp_revision"]),
                }
            )
        return items
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _cp_revisions(root: Path) -> list[dict[str, Any]]:
    rows = _transcript_rows(
        root,
        """
        SELECT projection_revision, covered_through_seq, strategy_id, strategy_version,
               snapshot_json, trace_ref, created_at
        FROM cp_revisions
        ORDER BY projection_revision
        """,
    )
    return [
        {
            "projection_revision": int(row["projection_revision"]),
            "covered_through_seq": int(row["covered_through_seq"]),
            "strategy_id": row["strategy_id"],
            "strategy_version": row["strategy_version"],
            "snapshot": json.loads(row["snapshot_json"]),
            "trace_ref": row["trace_ref"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _run_rows(root: Path) -> list[dict[str, Any]]:
    rows = _transcript_rows(
        root,
        """
        SELECT *
        FROM agent_runs
        ORDER BY rowid
        """,
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        decision = json.loads(row["decision_json"]) if row["decision_json"] is not None else None
        draft = (
            json.loads(row["draft_message_json"])
            if row["draft_message_json"] is not None
            else None
        )
        items.append(
            {
                "run_id": row["run_id"],
                "agent_id": row["agent_id"],
                "status": row["status"],
                "phase": row["phase"],
                "expected_conversation_seq": int(row["expected_conversation_seq"]),
                "causal_episode_id": row["causal_episode_id"],
                "caused_by_message_id": row["caused_by_message_id"],
                "agent_hop": int(row["agent_hop"]),
                "decision_json": decision,
                "draft_message_json": draft,
                "error_code": row["error_code"],
                "error_message": row["error_message"],
                "started_at": row["started_at"],
                "updated_at": row["updated_at"],
                "finished_at": row["finished_at"],
                "latency_ms": _latency_ms(row["started_at"], row["finished_at"]),
            }
        )
    return items


def _logs(root: Path) -> list[dict[str, Any]]:
    observations_path = root / "observations.ndjson"
    items: list[dict[str, Any]] = []
    if observations_path.exists():
        for line in observations_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = cast(dict[str, Any], json.loads(line))
            items.append(
                {
                    "id": payload.get("record_id"),
                    "timestamp": payload.get("observed_at_utc"),
                    "level": "info",
                    "phase": None,
                    "message": str(
                        payload.get("payload", {}).get("event_type")
                        or payload.get("payload")
                    ),
                    "agent_id": None,
                    "run_id": None,
                    "attempt_id": None,
                    "details": payload.get("payload"),
                }
            )
    return items


def _manual_score_path(root: Path) -> Path:
    return _analysis_dir(root) / MANUAL_SCORE_FILENAME


def _automatic_metrics_path(root: Path) -> Path:
    return _analysis_dir(root) / AUTOMATIC_METRICS_FILENAME


def load_manual_score(conversation: ConversationRecord) -> dict[str, Any]:
    root = _require_archive_root(conversation)
    path = _manual_score_path(root)
    if not path.exists():
        return {
            "schema_version": 1,
            "conversation_id": conversation.id,
            "rubric_version": "manual-score-v1",
            "updated_at": None,
            "scores": [],
            "overall_score": None,
            "overall_note": "",
        }
    return _json_read(path)


def save_manual_score(
    conversation: ConversationRecord,
    *,
    rubric_version: str,
    scores: list[dict[str, Any]],
    overall_note: str,
) -> dict[str, Any]:
    root = _require_archive_root(conversation)
    total = [float(item["score"]) for item in scores if item.get("score") is not None]
    payload = {
        "schema_version": 1,
        "conversation_id": conversation.id,
        "rubric_version": rubric_version,
        "updated_at": utc_now().isoformat(),
        "scores": scores,
        "overall_score": round(sum(total) / len(total), 3) if total else None,
        "overall_note": overall_note,
    }
    _json_write(_manual_score_path(root), payload)
    return payload


def compute_automatic_metrics(conversation: ConversationRecord) -> dict[str, Any]:
    root = _require_archive_root(conversation)
    messages = _public_messages(root)
    runs = _run_rows(root)
    run_rows = _transcript_rows(root, "SELECT * FROM agent_runs ORDER BY rowid")
    cp_revisions = _cp_revisions(root)
    logs = _logs(root)
    message_count = len(messages)
    agent_messages = [item for item in messages if item["sender_kind"] == "agent"]
    user_messages = [item for item in messages if item["sender_kind"] == "user"]
    committed_runs = [item for item in runs if item["status"] == "COMMITTED"]
    invalidated_runs = [item for item in runs if item["status"] == "INVALIDATED"]
    profile = ExperimentProfile.model_validate(
        conversation.locked_profile_json or conversation.draft_profile_json
    )
    token_total = 0
    cost_total = 0.0
    latency_values: list[int] = []
    for run_row, run in zip(run_rows, runs, strict=True):
        latency = run["latency_ms"]
        if latency is not None:
            latency_values.append(int(latency))
        for attempt in attempts_for_run(run_row, profile=profile):
            token_total += int(attempt["total_tokens"])
            cost_total = round(cost_total + float(attempt["cost_usd"]), 6)
    payload = {
        "schema_version": 1,
        "conversation_id": conversation.id,
        "computed_at": utc_now().isoformat(),
        "metrics": {
            "message_count": message_count,
            "user_message_count": len(user_messages),
            "agent_message_count": len(agent_messages),
            "cp_revision_count": len(cp_revisions),
            "run_count": len(runs),
            "committed_run_count": len(committed_runs),
            "invalidated_run_count": len(invalidated_runs),
            "log_entry_count": len(logs),
            "total_attempt_tokens": token_total,
            "total_cost_usd": round(cost_total, 6),
            "average_run_latency_ms": (
                round(sum(latency_values) / len(latency_values), 2)
                if latency_values
                else None
            ),
            "max_agent_hop": max((int(item["agent_hop"]) for item in messages), default=0),
        },
    }
    _json_write(_automatic_metrics_path(root), payload)
    return payload


def list_history_entries(settings: Settings) -> list[dict[str, Any]]:
    known_module_ids = _known_module_ids()
    items: list[dict[str, Any]] = []
    for manifest in scan_manifests(settings):
        modules = cast(dict[str, Any], manifest.get("draft_profile", {}).get("modules", {}))
        adapter_module_id = None
        if isinstance(modules.get("model_adapter"), dict):
            adapter_module_id = modules["model_adapter"].get("module_id")
        items.append(
            {
                "conversation_id": manifest["conversation_id"],
                "title": manifest.get("title"),
                "status": manifest.get("status"),
                "created_at": manifest.get("created_at"),
                "ended_at": manifest.get("ended_at"),
                "archive_dir": manifest.get("archive_dir"),
                "manifest_path": manifest.get("manifest_path"),
                "profile_hash": manifest.get("profile_hash"),
                "adapter_module_id": adapter_module_id,
                "adapter_known": (
                    adapter_module_id in known_module_ids if adapter_module_id else False
                ),
            }
        )
    items.sort(key=lambda item: str(item.get("ended_at") or item.get("created_at")), reverse=True)
    return items


def get_history_detail(settings: Settings, conversation_id: str) -> dict[str, Any]:
    manifest = _manifest_for_conversation(settings, conversation_id)
    root = _archive_root_from_manifest(manifest)
    entry = next(
        item
        for item in list_history_entries(settings)
        if item["conversation_id"] == conversation_id
    )
    return {
        "entry": entry,
        "raw_manifest": redact_payload(manifest),
        "messages": redact_payload(_public_messages(root)),
        "cp_revisions": redact_payload(_cp_revisions(root)),
        "logs": redact_payload(_logs(root)),
    }


def _job_dir(root: Path) -> Path:
    path = _exports_dir(root) / EXPORT_JOBS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(root: Path, job_id: str) -> Path:
    return _job_dir(root) / f"{job_id}.json"


def _job_payload(
    *,
    job_id: str,
    conversation_id: str,
    status: str,
    download_path: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "conversation_id": conversation_id,
        "status": status,
        "created_at": utc_now().isoformat(),
        "updated_at": utc_now().isoformat(),
        "download_path": download_path,
        "error": error,
    }


def _write_job(root: Path, payload: dict[str, Any]) -> None:
    _json_write(_job_path(root, str(payload["job_id"])), payload)


def get_analysis_export_job(conversation: ConversationRecord, job_id: str) -> dict[str, Any]:
    root = _require_archive_root(conversation)
    path = _job_path(root, job_id)
    if not path.exists():
        raise AppError(
            code="analysis_export_not_found",
            status_code=404,
            message="Analysis export job not found.",
        )
    return _json_read(path)


def _analysis_bundle_payload(
    conversation: ConversationRecord,
    settings: Settings,
) -> dict[str, bytes]:
    history = get_history_detail(settings, conversation.id)
    auto_metrics = compute_automatic_metrics(conversation)
    manual_score = load_manual_score(conversation)
    raw_manifest = cast(dict[str, Any], history["raw_manifest"])
    public_payload = {
        "conversation": {
            "conversation_id": conversation.id,
            "title": raw_manifest.get("title"),
            "status": raw_manifest.get("status"),
            "profile_hash": raw_manifest.get("profile_hash"),
            "created_at": raw_manifest.get("created_at"),
            "ended_at": raw_manifest.get("ended_at"),
        },
        "messages": history["messages"],
        "cp_revisions": history["cp_revisions"],
        "logs": history["logs"],
        "automatic_metrics": auto_metrics,
        "manual_score": manual_score,
    }
    files: dict[str, Any] = {
        "manifest/raw.json": redact_payload(raw_manifest),
        "public/history.json": redact_payload(public_payload),
        "public/messages.json": redact_payload(history["messages"]),
        "public/cp-revisions.json": redact_payload(history["cp_revisions"]),
        "public/logs.json": redact_payload(history["logs"]),
        "analysis/automatic-metrics.json": redact_payload(auto_metrics),
        "analysis/manual-score.json": redact_payload(manual_score),
    }
    rendered = {
        path: (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        for path, payload in files.items()
    }
    manifest_payload = {
        "analysis_bundle_version": EXPORT_MANIFEST_VERSION,
        "conversation_id": conversation.id,
        "generated_at": utc_now().isoformat(),
        "application_version": settings.app_version,
        "archive_schema_version": raw_manifest.get("archive_schema_version"),
        "files": [
            {
                "path": path,
                "size_bytes": len(content),
                "sha256": _sha256_bytes(content),
            }
            for path, content in sorted(rendered.items())
        ],
    }
    rendered["manifest.json"] = (
        json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return rendered


def _generate_analysis_zip(
    *,
    conversation: ConversationRecord,
    settings: Settings,
    job_id: str,
) -> None:
    root = _require_archive_root(conversation)
    job = get_analysis_export_job(conversation, job_id)
    try:
        files = _analysis_bundle_payload(conversation, settings)
        zip_path = _exports_dir(root) / f"analysis-{job_id}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, content in sorted(files.items()):
                archive.writestr(path, content)
        job.update(
            {
                "status": "ready",
                "updated_at": utc_now().isoformat(),
                "download_path": str(zip_path),
            }
        )
    except Exception as exc:
        job.update(
            {
                "status": "failed",
                "updated_at": utc_now().isoformat(),
                "error": str(exc),
            }
        )
    _write_job(root, job)


def create_analysis_export_job(
    conversation: ConversationRecord,
    *,
    settings: Settings,
) -> dict[str, Any]:
    from pal_chat_server.ids import generate_ulid

    root = _require_archive_root(conversation)
    job_id = generate_ulid()
    payload = _job_payload(
        job_id=job_id,
        conversation_id=conversation.id,
        status="queued",
        download_path=None,
    )
    _write_job(root, payload)

    def runner() -> None:
        with _EXPORT_LOCK:
            current = get_analysis_export_job(conversation, job_id)
            current["status"] = "running"
            current["updated_at"] = utc_now().isoformat()
            _write_job(root, current)
            _generate_analysis_zip(
                conversation=conversation,
                settings=settings,
                job_id=job_id,
            )

    threading.Thread(target=runner, daemon=True).start()
    return payload


def analysis_export_download_path(conversation: ConversationRecord, job_id: str) -> Path:
    job = get_analysis_export_job(conversation, job_id)
    if job["status"] != "ready":
        raise AppError(
            code="analysis_export_not_ready",
            status_code=409,
            message="Analysis export is not ready.",
        )
    root = _require_archive_root(conversation)
    exports_root = _exports_dir(root).resolve()
    path = (exports_root / f"analysis-{job_id}.zip").resolve()
    expected_name = f"analysis-{job_id}.zip"
    try:
        path.relative_to(exports_root)
    except ValueError as exc:
        raise AppError(
            code="analysis_export_invalid_path",
            status_code=409,
            message="Analysis export path is invalid.",
        ) from exc
    if path.name != expected_name or path.suffix != ".zip":
        raise AppError(
            code="analysis_export_invalid_path",
            status_code=409,
            message="Analysis export path is invalid.",
        )
    if not path.exists():
        raise AppError(
            code="analysis_export_missing",
            status_code=404,
            message="Analysis export file is missing.",
        )
    if path.is_symlink():
        raise AppError(
            code="analysis_export_invalid_file",
            status_code=409,
            message="Analysis export file is invalid.",
        )
    file_stat = path.stat()
    if not stat.S_ISREG(file_stat.st_mode) or not zipfile.is_zipfile(path):
        raise AppError(
            code="analysis_export_invalid_file",
            status_code=409,
            message="Analysis export file is invalid.",
        )
    return path
