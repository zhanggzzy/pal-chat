from __future__ import annotations

import json
import sqlite3
from typing import Any

from pal_chat_server.errors import AppError
from pal_chat_server.ids import generate_ulid
from pal_chat_server.models import ConversationRecord
from pal_chat_server.schemas import ExperimentProfile
from pal_chat_server.sequence_runtime import _CONVERSATION_LOCKS, connect_transcript, utc_now


def _agent_binding(profile: ExperimentProfile, agent_id: str) -> tuple[str, str]:
    if agent_id == profile.agent_a.agent_id:
        binding = profile.agent_a.model
    elif agent_id == profile.agent_b.agent_id:
        binding = profile.agent_b.model
    else:
        raise AppError(
            code="unknown_agent",
            status_code=409,
            message="Unknown agent sender.",
        )
    return binding.provider, binding.model


def _attempt_cost(provider: str, total_tokens: int, explicit_cost: object | None) -> float:
    if explicit_cost is not None:
        return round(float(str(explicit_cost)), 6)
    if provider == "scripted":
        return 0.0
    return round(total_tokens * 0.000002, 6)


def _attempt_row_to_read(row: sqlite3.Row) -> dict[str, Any]:
    payload = json.loads(row["payload_json"]) if row["payload_json"] else None
    return {
        "attempt_id": row["attempt_id"],
        "run_id": row["run_id"],
        "agent_id": row["agent_id"],
        "phase": row["phase"],
        "provider": row["provider"],
        "model": row["model"],
        "prompt_tokens": int(row["prompt_tokens"]),
        "completion_tokens": int(row["completion_tokens"]),
        "total_tokens": int(row["total_tokens"]),
        "cost_usd": round(float(row["cost_usd"]), 6),
        "latency_ms": _latency_ms(row["started_at"], row["finished_at"]),
        "bundle_revision": row["bundle_revision"],
        "memory_revision_before": row["memory_revision_before"],
        "memory_revision_after": row["memory_revision_after"],
        "staged_memory_revision": row["staged_memory_revision"],
        "payload": payload,
        "created_at": row["started_at"],
        "status": row["status"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "retryable": bool(row["retryable"]),
        "backoff_ms": int(row["backoff_ms"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


def _parse_time(value: str | None) -> Any:
    if value is None:
        return None
    from datetime import UTC, datetime

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


def _budget_limits(conversation: ConversationRecord) -> tuple[int, int]:
    guardrails = conversation.guardrails_json
    return int(guardrails.get("max_llm_calls", 32)), int(
        guardrails.get("max_total_tokens", 64_000)
    )


def register_attempt(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    agent_id: str,
    run_id: str,
    phase: str,
    bundle_revision: str | None,
    memory_revision_before: str | None,
    staged_memory_revision: str | None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lock = _CONVERSATION_LOCKS[conversation.id]
    provider, model = _agent_binding(profile, agent_id)
    max_llm_calls, max_total_tokens = _budget_limits(conversation)
    with lock, connect_transcript(conversation) as connection:
        connection.execute("BEGIN IMMEDIATE")
        run_row = connection.execute(
            """
            SELECT run_id, agent_id
            FROM agent_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        if run_row is None:
            connection.rollback()
            raise AppError(
                code="run_not_found",
                status_code=409,
                message="Run not found for attempt registration.",
            )
        if str(run_row["agent_id"]) != agent_id:
            connection.rollback()
            raise AppError(
                code="internal_auth_failed",
                status_code=403,
                message="Attempt registration agent mismatch.",
            )
        now = utc_now().isoformat()
        ledger_row = connection.execute(
            """
            SELECT used_llm_calls, used_total_tokens, used_total_cost_usd
            FROM conversation_budget_ledger
            WHERE conversation_id = ?
            """,
            (conversation.id,),
        ).fetchone()
        used_llm_calls = 0 if ledger_row is None else int(ledger_row["used_llm_calls"])
        used_total_tokens = 0 if ledger_row is None else int(ledger_row["used_total_tokens"])
        phase_ordinal = (
            int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(phase_ordinal), 0) AS max_ordinal
                    FROM llm_attempts
                    WHERE run_id = ? AND phase = ?
                    """,
                    (run_id, phase),
                ).fetchone()["max_ordinal"]
            )
            + 1
        )
        attempt_id = generate_ulid()
        admitted = used_llm_calls < max_llm_calls and used_total_tokens < max_total_tokens
        status = "started" if admitted else "rejected"
        error_code = None if admitted else "budget_exhausted"
        error_message = None if admitted else "Conversation LLM budget exhausted."
        error_class = None if admitted else "budget"
        connection.execute(
            """
            INSERT INTO llm_attempts(
              attempt_id, run_id, agent_id, phase, phase_ordinal, provider, model,
              status, profile_hash, bundle_revision, memory_revision_before,
              memory_revision_after, staged_memory_revision, guardrails_json,
              payload_json, error_code, error_message, error_class, retryable,
              backoff_ms, prompt_tokens, completion_tokens, total_tokens, cost_usd,
              started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                run_id,
                agent_id,
                phase,
                phase_ordinal,
                provider,
                model,
                status,
                conversation.profile_hash or "",
                bundle_revision,
                memory_revision_before,
                None,
                staged_memory_revision,
                json.dumps(conversation.guardrails_json, ensure_ascii=False, sort_keys=True),
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    if payload is not None
                    else None
                ),
                error_code,
                error_message,
                error_class,
                0,
                0,
                0,
                0,
                0,
                0.0,
                now,
                None if admitted else now,
            ),
        )
        if admitted:
            connection.execute(
                """
                INSERT INTO conversation_budget_ledger(
                  conversation_id, used_llm_calls, used_total_tokens,
                  used_total_cost_usd, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(conversation_id) DO UPDATE SET
                  used_llm_calls = excluded.used_llm_calls,
                  used_total_tokens = excluded.used_total_tokens,
                  used_total_cost_usd = excluded.used_total_cost_usd,
                  updated_at = excluded.updated_at
                """,
                (
                    conversation.id,
                    used_llm_calls + 1,
                    used_total_tokens,
                    0.0 if ledger_row is None else float(ledger_row["used_total_cost_usd"]),
                    now,
                ),
            )
        connection.commit()
    remaining_calls = max(0, max_llm_calls - used_llm_calls - (1 if admitted else 0))
    remaining_tokens = max(0, max_total_tokens - used_total_tokens)
    return {
        "attempt_id": attempt_id,
        "admitted": admitted,
        "status": status,
        "budget": {
            "max_llm_calls": max_llm_calls,
            "max_total_tokens": max_total_tokens,
            "used_llm_calls": used_llm_calls + (1 if admitted else 0),
            "used_total_tokens": used_total_tokens,
            "remaining_llm_calls": remaining_calls,
            "remaining_total_tokens": remaining_tokens,
        },
        "error": None
        if admitted
        else {
            "code": "budget_exhausted",
            "message": "Conversation LLM budget exhausted.",
        },
    }


def finalize_attempt(
    conversation: ConversationRecord,
    *,
    attempt_id: str,
    status: str,
    payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_class: str | None = None,
    retryable: bool = False,
    backoff_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cost_usd: float | None = None,
    memory_revision_after: str | None = None,
) -> dict[str, Any]:
    lock = _CONVERSATION_LOCKS[conversation.id]
    with lock, connect_transcript(conversation) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT *
            FROM llm_attempts
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            connection.rollback()
            raise AppError(
                code="attempt_not_found",
                status_code=409,
                message="Attempt not found.",
            )
        if str(row["status"]) != "started":
            connection.commit()
            return _attempt_row_to_read(row)
        now = utc_now().isoformat()
        total_cost = _attempt_cost(str(row["provider"]), total_tokens, cost_usd)
        connection.execute(
            """
            UPDATE llm_attempts
            SET status = ?,
                payload_json = ?,
                error_code = ?,
                error_message = ?,
                error_class = ?,
                retryable = ?,
                backoff_ms = ?,
                prompt_tokens = ?,
                completion_tokens = ?,
                total_tokens = ?,
                cost_usd = ?,
                memory_revision_after = ?,
                finished_at = ?,
                guardrails_json = ?
            WHERE attempt_id = ?
            """,
            (
                status,
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True)
                    if payload is not None
                    else None
                ),
                error_code,
                error_message,
                error_class,
                1 if retryable else 0,
                backoff_ms,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                total_cost,
                memory_revision_after,
                now,
                json.dumps(conversation.guardrails_json, ensure_ascii=False, sort_keys=True),
                attempt_id,
            ),
        )
        ledger_row = connection.execute(
            """
            SELECT used_llm_calls, used_total_tokens, used_total_cost_usd
            FROM conversation_budget_ledger
            WHERE conversation_id = ?
            """,
            (conversation.id,),
        ).fetchone()
        used_llm_calls = 0 if ledger_row is None else int(ledger_row["used_llm_calls"])
        used_total_tokens = 0 if ledger_row is None else int(ledger_row["used_total_tokens"])
        used_total_cost = 0.0 if ledger_row is None else float(ledger_row["used_total_cost_usd"])
        connection.execute(
            """
            INSERT INTO conversation_budget_ledger(
              conversation_id, used_llm_calls, used_total_tokens, used_total_cost_usd, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
              used_llm_calls = excluded.used_llm_calls,
              used_total_tokens = excluded.used_total_tokens,
              used_total_cost_usd = excluded.used_total_cost_usd,
              updated_at = excluded.updated_at
            """,
            (
                conversation.id,
                used_llm_calls,
                used_total_tokens + total_tokens,
                round(used_total_cost + total_cost, 6),
                now,
            ),
        )
        updated_row = connection.execute(
            """
            SELECT *
            FROM llm_attempts
            WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        assert updated_row is not None
        connection.commit()
    return _attempt_row_to_read(updated_row)


def list_attempt_rows(
    conversation: ConversationRecord,
    *,
    agent_id: str | None = None,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[object] = []
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    with connect_transcript(conversation) as connection:
        rows = connection.execute(
            f"""
            SELECT *
            FROM llm_attempts
            WHERE {' AND '.join(clauses)}
            ORDER BY started_at, attempt_id
            """,
            tuple(params),
        ).fetchall()
    return [_attempt_row_to_read(row) for row in rows]


def attempts_for_run(
    conversation: ConversationRecord,
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    with connect_transcript(conversation) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM llm_attempts
            WHERE run_id = ?
            ORDER BY started_at, attempt_id
            """,
            (run_id,),
        ).fetchall()
    return [_attempt_row_to_read(row) for row in rows]


def mark_run_attempts_crashed(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    finished_at: str,
    error_code: str,
    error_message: str,
) -> None:
    connection.execute(
        """
        UPDATE llm_attempts
        SET status = 'crashed',
            error_code = ?,
            error_message = ?,
            error_class = 'crash',
            retryable = 0,
            backoff_ms = 0,
            finished_at = COALESCE(finished_at, ?)
        WHERE run_id = ?
          AND status = 'started'
        """,
        (error_code, error_message, finished_at, run_id),
    )


def budget_snapshot(conversation: ConversationRecord) -> dict[str, Any]:
    max_llm_calls, max_total_tokens = _budget_limits(conversation)
    with connect_transcript(conversation) as connection:
        row = connection.execute(
            """
            SELECT used_llm_calls, used_total_tokens, used_total_cost_usd
            FROM conversation_budget_ledger
            WHERE conversation_id = ?
            """,
            (conversation.id,),
        ).fetchone()
    used_llm_calls = 0 if row is None else int(row["used_llm_calls"])
    used_total_tokens = 0 if row is None else int(row["used_total_tokens"])
    used_total_cost = 0.0 if row is None else float(row["used_total_cost_usd"])
    return {
        "max_llm_calls": max_llm_calls,
        "max_total_tokens": max_total_tokens,
        "used_llm_calls": used_llm_calls,
        "used_total_tokens": used_total_tokens,
        "used_total_cost_usd": round(used_total_cost, 6),
    }
