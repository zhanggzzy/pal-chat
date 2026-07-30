from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from pal_chat_server.attempts import attempts_for_run as ledger_attempts_for_run
from pal_chat_server.attempts import list_attempt_rows
from pal_chat_server.errors import AppError
from pal_chat_server.models import ConversationRecord
from pal_chat_server.schemas import ExperimentProfile
from pal_chat_server.sequence_runtime import connect_transcript


def _require_archive(conversation: ConversationRecord) -> Path:
    if conversation.archive_dir is None:
        raise AppError(
            code="archive_missing",
            status_code=409,
            message="Conversation archive is missing.",
        )
    return Path(conversation.archive_dir)


def _agent_ids(profile: ExperimentProfile) -> set[str]:
    return {profile.agent_a.agent_id, profile.agent_b.agent_id}


def ensure_agent_id(profile: ExperimentProfile, agent_id: str) -> None:
    if agent_id not in _agent_ids(profile):
        raise AppError(
            code="agent_not_found",
            status_code=404,
            message="Agent not found in conversation profile.",
        )


def _agent_state_dir(
    conversation: ConversationRecord,
    profile: ExperimentProfile,
    agent_id: str,
) -> Path:
    ensure_agent_id(profile, agent_id)
    return _require_archive(conversation) / "module-state" / agent_id


def _read_json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _maybe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _read_json(path)


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
    delta = int((end - start).total_seconds() * 1000)
    return max(delta, 0)


def _attempt_cost(provider: str, total_tokens: int) -> float:
    if provider == "scripted":
        return 0.0
    return round(total_tokens * 0.000002, 6)


def _phase_attempt(
    *,
    run_row: sqlite3.Row,
    trace: dict[str, Any] | None,
    phase: str,
    ordinal: int,
    provider: str,
    model: str,
    total_latency_ms: int | None,
) -> dict[str, Any] | None:
    if trace is None:
        return None
    total_tokens = int(trace.get("estimated_tokens") or 0)
    latency_ms = None
    if total_latency_ms is not None:
        latency_ms = max(total_latency_ms // 2, 1)
    cost_usd = _attempt_cost(provider, total_tokens)
    return {
        "attempt_id": f"{run_row['run_id']}:{phase}:{ordinal}",
        "run_id": run_row["run_id"],
        "agent_id": run_row["agent_id"],
        "phase": phase,
        "provider": provider,
        "model": model,
        "prompt_tokens": total_tokens,
        "completion_tokens": 0,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "latency_ms": latency_ms,
        "bundle_revision": trace.get("bundle_revision"),
        "memory_revision_before": trace.get("memory_revision_before"),
        "memory_revision_after": trace.get("memory_revision_after"),
        "staged_memory_revision": trace.get("staged_memory_revision"),
        "payload": trace.get("payload"),
        "created_at": run_row["updated_at"],
    }


def attempts_for_run(
    run_row: sqlite3.Row,
    *,
    conversation: ConversationRecord | None = None,
    profile: ExperimentProfile,
) -> list[dict[str, Any]]:
    if conversation is not None:
        ledger_items = ledger_attempts_for_run(conversation, run_id=str(run_row["run_id"]))
        if ledger_items:
            return ledger_items
    agent_profile = (
        profile.agent_a if run_row["agent_id"] == profile.agent_a.agent_id else profile.agent_b
    )
    provider = agent_profile.model.provider
    model = agent_profile.model.model
    decision = (
        json.loads(run_row["decision_json"]) if run_row["decision_json"] is not None else None
    )
    draft = (
        json.loads(run_row["draft_message_json"])
        if run_row["draft_message_json"] is not None
        else None
    )
    latency = _latency_ms(run_row["started_at"], run_row["finished_at"])
    attempts: list[dict[str, Any]] = []
    decision_attempt = _phase_attempt(
        run_row=run_row,
        trace=cast(dict[str, Any] | None, decision),
        phase="decision",
        ordinal=1,
        provider=provider,
        model=model,
        total_latency_ms=latency,
    )
    if decision_attempt is not None:
        attempts.append(decision_attempt)
    action_attempt = _phase_attempt(
        run_row=run_row,
        trace=cast(dict[str, Any] | None, draft),
        phase="action",
        ordinal=2,
        provider=provider,
        model=model,
        total_latency_ms=latency,
    )
    if action_attempt is not None:
        attempts.append(action_attempt)
    return attempts


def build_run_record(
    run_row: sqlite3.Row,
    *,
    conversation: ConversationRecord,
    profile: ExperimentProfile,
) -> dict[str, Any]:
    return {
        "run_id": run_row["run_id"],
        "agent_id": run_row["agent_id"],
        "status": run_row["status"],
        "phase": run_row["phase"],
        "observation_message_ids": json.loads(run_row["observation_message_ids_json"]),
        "root_message_ids": json.loads(run_row["root_message_ids_json"]),
        "expected_conversation_seq": int(run_row["expected_conversation_seq"]),
        "profile_hash": run_row["profile_hash"],
        "idempotency_key": run_row["idempotency_key"],
        "causal_episode_id": run_row["causal_episode_id"],
        "caused_by_message_id": run_row["caused_by_message_id"],
        "agent_hop": int(run_row["agent_hop"]),
        "decision_json": (
            json.loads(run_row["decision_json"]) if run_row["decision_json"] is not None else None
        ),
        "draft_message_json": (
            json.loads(run_row["draft_message_json"])
            if run_row["draft_message_json"] is not None
            else None
        ),
        "error_code": run_row["error_code"],
        "error_message": run_row["error_message"],
        "started_at": run_row["started_at"],
        "updated_at": run_row["updated_at"],
        "finished_at": run_row["finished_at"],
        "latency_ms": _latency_ms(run_row["started_at"], run_row["finished_at"]),
        "attempts": attempts_for_run(run_row, conversation=conversation, profile=profile),
    }


def list_runs(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    agent_id: str | None,
    run_status: str | None,
    limit: int,
    after: str | None,
) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[object] = []
    if agent_id is not None:
        ensure_agent_id(profile, agent_id)
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if run_status is not None:
        clauses.append("status = ?")
        params.append(run_status)
    if after is not None:
        clauses.append("run_id < ?")
        params.append(after)
    params.append(limit)
    with connect_transcript(conversation) as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM agent_runs
            WHERE {' AND '.join(clauses)}
            ORDER BY started_at DESC, run_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    return [build_run_record(row, conversation=conversation, profile=profile) for row in rows]


def get_run(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    run_id: str,
) -> dict[str, Any]:
    with connect_transcript(conversation) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM agent_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
    if row is None:
        raise AppError(code="run_not_found", status_code=404, message="Run not found.")
    return build_run_record(row, conversation=conversation, profile=profile)


def list_memory_revisions(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    agent_id: str,
) -> list[dict[str, Any]]:
    state_dir = _agent_state_dir(conversation, profile, agent_id)
    revision_dir = state_dir / "memory" / "revisions"
    items = [_read_json(path) for path in sorted(revision_dir.glob("mem-*.json"))]
    items.sort(key=lambda item: int(str(item["revision"]).split("-")[-1]), reverse=True)
    return items


def get_memory_revision(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    agent_id: str,
    revision: str,
) -> dict[str, Any]:
    state_dir = _agent_state_dir(conversation, profile, agent_id)
    path = state_dir / "memory" / "revisions" / f"{revision}.json"
    if not path.exists():
        raise AppError(
            code="memory_revision_not_found",
            status_code=404,
            message="Memory revision not found.",
        )
    return _read_json(path)


def list_context_bundles(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    agent_id: str,
    run_id: str | None,
) -> list[dict[str, Any]]:
    state_dir = _agent_state_dir(conversation, profile, agent_id)
    items = [
        _read_json(path)
        for path in sorted((state_dir / "context-bundles").glob("ctx-*.json"))
    ]
    if run_id is not None:
        run = get_run(conversation, profile=profile, run_id=run_id)
        bundle_revisions: set[str] = set()
        for attempt in cast(list[dict[str, Any]], run["attempts"]):
            bundle_revision = attempt.get("bundle_revision")
            if bundle_revision:
                bundle_revisions.add(str(bundle_revision))
        items = [item for item in items if str(item.get("revision")) in bundle_revisions]
    items.sort(key=lambda item: int(str(item["revision"]).split("-")[-1]), reverse=True)
    return items


def list_attempts(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    agent_id: str,
    run_id: str | None,
) -> list[dict[str, Any]]:
    ensure_agent_id(profile, agent_id)
    attempts = list_attempt_rows(conversation, agent_id=agent_id, run_id=run_id)
    if attempts:
        return attempts
    with connect_transcript(conversation) as connection:
        clauses = ["agent_id = ?"]
        params: list[object] = [agent_id]
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        rows = connection.execute(
            f"""
            SELECT *
            FROM agent_runs
            WHERE {' AND '.join(clauses)}
            ORDER BY started_at DESC
            """,
            tuple(params),
        ).fetchall()
    fallback_attempts: list[dict[str, Any]] = []
    for row in rows:
        fallback_attempts.extend(attempts_for_run(row, conversation=conversation, profile=profile))
    return fallback_attempts


def list_causal_episodes(conversation: ConversationRecord) -> list[dict[str, Any]]:
    with connect_transcript(conversation) as connection:
        rows = connection.execute(
            """
            SELECT episode_id, root_message_ids_json, total_actions, agent_a_actions,
                   agent_b_actions, max_agent_hop, updated_at
            FROM causal_episode_budget
            ORDER BY updated_at DESC, episode_id DESC
            """
        ).fetchall()
    return [
        {
            "episode_id": row["episode_id"],
            "root_message_ids": json.loads(row["root_message_ids_json"]),
            "total_actions": int(row["total_actions"]),
            "agent_a_actions": int(row["agent_a_actions"]),
            "agent_b_actions": int(row["agent_b_actions"]),
            "max_agent_hop": int(row["max_agent_hop"]),
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_cost_breakdown(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
) -> dict[str, Any]:
    by_agent: dict[str, dict[str, float | int]] = {}
    by_phase: dict[str, dict[str, float | int]] = {}
    total_tokens = 0
    total_cost = 0.0
    for run in list_runs(
        conversation,
        profile=profile,
        agent_id=None,
        run_status=None,
        limit=500,
        after=None,
    ):
        for attempt in cast(list[dict[str, Any]], run["attempts"]):
            agent_bucket = by_agent.setdefault(
                str(attempt["agent_id"]),
                {"tokens": 0, "cost_usd": 0.0},
            )
            phase_bucket = by_phase.setdefault(
                str(attempt["phase"]),
                {"tokens": 0, "cost_usd": 0.0},
            )
            tokens = int(attempt["total_tokens"])
            cost = float(attempt["cost_usd"])
            agent_bucket["tokens"] = int(agent_bucket["tokens"]) + tokens
            agent_bucket["cost_usd"] = round(float(agent_bucket["cost_usd"]) + cost, 6)
            phase_bucket["tokens"] = int(phase_bucket["tokens"]) + tokens
            phase_bucket["cost_usd"] = round(float(phase_bucket["cost_usd"]) + cost, 6)
            total_tokens += tokens
            total_cost = round(total_cost + cost, 6)
    return {
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
        "by_agent": by_agent,
        "by_phase": by_phase,
    }


def list_logs(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    run_id: str | None,
    phase: str | None,
    level: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    del profile
    archive_root = _require_archive(conversation)
    entries: list[dict[str, Any]] = []
    observations_path = archive_root / "observations.ndjson"
    if observations_path.exists():
        for line in observations_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = cast(dict[str, Any], json.loads(line))
            entry = {
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
            entries.append(entry)
    with connect_transcript(conversation) as connection:
        rows = connection.execute(
            """
            SELECT run_id, agent_id, status, phase, error_code, error_message, updated_at,
                   decision_json, draft_message_json
            FROM agent_runs
            ORDER BY updated_at DESC
            """
        ).fetchall()
    for row in rows:
        entries.append(
            {
                "id": f"{row['run_id']}:status",
                "timestamp": row["updated_at"],
                "level": "error" if row["error_code"] else "info",
                "phase": row["phase"],
                "message": f"run {row['status']}",
                "agent_id": row["agent_id"],
                "run_id": row["run_id"],
                "attempt_id": None,
                "details": {
                    "error_code": row["error_code"],
                    "error_message": row["error_message"],
                    "decision_json": (
                        json.loads(row["decision_json"])
                        if row["decision_json"] is not None
                        else None
                    ),
                    "draft_message_json": (
                        json.loads(row["draft_message_json"])
                        if row["draft_message_json"] is not None
                        else None
                    ),
                },
            }
        )
    if run_id is not None:
        entries = [entry for entry in entries if entry["run_id"] == run_id]
    if phase is not None:
        entries = [entry for entry in entries if entry["phase"] == phase]
    if level is not None:
        entries = [entry for entry in entries if entry["level"] == level]
    entries.sort(key=lambda item: str(item["timestamp"]), reverse=True)
    return entries[:limit]
