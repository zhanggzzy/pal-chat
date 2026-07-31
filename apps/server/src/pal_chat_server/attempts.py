from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from pal_chat_server.contracts import ModelAdapter, ModelRequest, ModelResponse
from pal_chat_server.errors import AppError
from pal_chat_server.ids import generate_ulid
from pal_chat_server.models import ConversationRecord
from pal_chat_server.schemas import ExperimentProfile
from pal_chat_server.sequence_runtime import _CONVERSATION_LOCKS, connect_transcript, utc_now


@dataclass(slots=True)
class InvocationContext:
    owner_kind: str
    owner_id: str
    phase: str
    profile_hash: str
    run_id: str | None = None
    agent_id: str | None = None
    bundle_revision: str | None = None
    memory_revision_before: str | None = None
    staged_memory_revision: str | None = None
    parent_attempt_id: str | None = None
    payload: dict[str, Any] | None = None
    estimated_input_tokens: int = 0
    max_output_tokens: int = 0


class AdmissionRejectedError(Exception):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvocationError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        error_class: str,
        backoff_ms: int = 0,
        payload: dict[str, Any] | None = None,
        attempt_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.error_class = error_class
        self.backoff_ms = backoff_ms
        self.payload = payload or {}
        self.attempt_id = attempt_id


def _agent_binding(profile: ExperimentProfile, agent_id: str | None) -> tuple[str, str]:
    if agent_id is None:
        binding = profile.agent_a.model
    elif agent_id == profile.agent_a.agent_id:
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
        "parent_attempt_id": row["parent_attempt_id"],
        "owner_kind": row["owner_kind"],
        "owner_id": row["owner_id"],
        "reserved_tokens": int(row["reserved_tokens"]),
    }


def _classify_model_error(code: str) -> tuple[str, bool, int]:
    if code == "TIMEOUT":
        return ("timeout", True, 200)
    if code == "HTTP_429":
        return ("http_429", True, 200)
    if code.startswith("HTTP_5") or code == "RETRYABLE_5XX":
        return ("http_5xx", True, 300)
    return ("non_retryable", False, 0)


def _admission_decision(
    connection: sqlite3.Connection,
    conversation: ConversationRecord,
    *,
    reserve_tokens: int,
) -> tuple[bool, int, int, int, float]:
    max_llm_calls, max_total_tokens = _budget_limits(conversation)
    row = connection.execute(
        """
        SELECT used_llm_calls, used_total_tokens, reserved_total_tokens, used_total_cost_usd
        FROM conversation_budget_ledger
        WHERE conversation_id = ?
        """,
        (conversation.id,),
    ).fetchone()
    used_llm_calls = 0 if row is None else int(row["used_llm_calls"])
    used_total_tokens = 0 if row is None else int(row["used_total_tokens"])
    reserved_total_tokens = 0 if row is None else int(row["reserved_total_tokens"])
    used_total_cost_usd = 0.0 if row is None else float(row["used_total_cost_usd"])
    admitted = (
        used_llm_calls < max_llm_calls
        and used_total_tokens + reserved_total_tokens + reserve_tokens <= max_total_tokens
    )
    return (
        admitted,
        used_llm_calls,
        used_total_tokens,
        reserved_total_tokens,
        used_total_cost_usd,
    )


def _write_admission_event(
    connection: sqlite3.Connection,
    conversation: ConversationRecord,
    *,
    context: InvocationContext,
    decision: str,
    reserved_tokens: int,
    reason_code: str | None = None,
    reason_message: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO llm_admission_events(
          admission_id, owner_kind, owner_id, run_id, agent_id, phase, decision,
          reason_code, reason_message, reserved_tokens, profile_hash, guardrails_json,
          payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            generate_ulid(),
            context.owner_kind,
            context.owner_id,
            context.run_id,
            context.agent_id,
            context.phase,
            decision,
            reason_code,
            reason_message,
            reserved_tokens,
            context.profile_hash,
            json.dumps(conversation.guardrails_json, ensure_ascii=False, sort_keys=True),
            (
                json.dumps(context.payload, ensure_ascii=False, sort_keys=True)
                if context.payload is not None
                else None
            ),
            utc_now().isoformat(),
        ),
    )


def admit_attempt(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    context: InvocationContext,
) -> dict[str, Any]:
    lock = _CONVERSATION_LOCKS[conversation.id]
    provider, model = _agent_binding(profile, context.agent_id)
    reserve_tokens = max(1, context.estimated_input_tokens)
    with lock, connect_transcript(conversation) as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = utc_now().isoformat()
        (
            admitted,
            used_llm_calls,
            used_total_tokens,
            reserved_total_tokens,
            used_total_cost_usd,
        ) = _admission_decision(
            connection,
            conversation,
            reserve_tokens=reserve_tokens,
        )
        if not admitted:
            _write_admission_event(
                connection,
                conversation,
                context=context,
                decision="rejected",
                reserved_tokens=reserve_tokens,
                reason_code="budget_exhausted",
                reason_message="Conversation LLM budget exhausted.",
            )
            connection.commit()
            return {
                "admitted": False,
                "error": {
                    "code": "budget_exhausted",
                    "message": "Conversation LLM budget exhausted.",
                },
            }
        _write_admission_event(
            connection,
            conversation,
            context=context,
            decision="admitted",
            reserved_tokens=reserve_tokens,
        )
        phase_ordinal = int(
            connection.execute(
                """
                SELECT COALESCE(MAX(phase_ordinal), 0) AS max_ordinal
                FROM llm_attempts
                WHERE owner_kind = ? AND owner_id = ? AND phase = ?
                """,
                (context.owner_kind, context.owner_id, context.phase),
            ).fetchone()["max_ordinal"]
        ) + 1
        attempt_id = generate_ulid()
        connection.execute(
            """
            INSERT INTO llm_attempts(
              attempt_id, owner_kind, owner_id, run_id, agent_id, phase, phase_ordinal,
              parent_attempt_id, provider, model, status, profile_hash, bundle_revision,
              memory_revision_before, memory_revision_after, staged_memory_revision,
              guardrails_json, payload_json, error_code, error_message, error_class,
              retryable, backoff_ms, reserved_tokens, prompt_tokens, completion_tokens,
              total_tokens, cost_usd, started_at, finished_at
            ) VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                attempt_id,
                context.owner_kind,
                context.owner_id,
                context.run_id,
                context.agent_id,
                context.phase,
                phase_ordinal,
                context.parent_attempt_id,
                provider,
                model,
                "started",
                context.profile_hash,
                context.bundle_revision,
                context.memory_revision_before,
                None,
                context.staged_memory_revision,
                json.dumps(conversation.guardrails_json, ensure_ascii=False, sort_keys=True),
                (
                    json.dumps(context.payload, ensure_ascii=False, sort_keys=True)
                    if context.payload is not None
                    else None
                ),
                None,
                None,
                None,
                0,
                0,
                reserve_tokens,
                0,
                0,
                0,
                0.0,
                now,
                None,
            ),
        )
        connection.execute(
            """
            INSERT INTO conversation_budget_ledger(
              conversation_id, used_llm_calls, used_total_tokens, reserved_total_tokens,
              used_total_cost_usd, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
              used_llm_calls = excluded.used_llm_calls,
              used_total_tokens = excluded.used_total_tokens,
              reserved_total_tokens = excluded.reserved_total_tokens,
              used_total_cost_usd = excluded.used_total_cost_usd,
              updated_at = excluded.updated_at
            """,
            (
                conversation.id,
                used_llm_calls + 1,
                used_total_tokens,
                reserved_total_tokens + reserve_tokens,
                used_total_cost_usd,
                now,
            ),
        )
        connection.commit()
    max_llm_calls, max_total_tokens = _budget_limits(conversation)
    return {
        "admitted": True,
        "attempt_id": attempt_id,
        "budget": {
            "max_llm_calls": max_llm_calls,
            "max_total_tokens": max_total_tokens,
            "used_llm_calls": used_llm_calls + 1,
            "used_total_tokens": used_total_tokens,
            "reserved_total_tokens": reserved_total_tokens + reserve_tokens,
        },
    }


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
    return admit_attempt(
        conversation,
        profile=profile,
        context=InvocationContext(
            owner_kind="agent_run",
            owner_id=run_id,
            run_id=run_id,
            agent_id=agent_id,
            phase=phase,
            profile_hash=conversation.profile_hash or "",
            bundle_revision=bundle_revision,
            memory_revision_before=memory_revision_before,
            staged_memory_revision=staged_memory_revision,
            payload=payload,
            estimated_input_tokens=len(
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
            )
            // 4,
            max_output_tokens=256,
        ),
    )


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
            "SELECT * FROM llm_attempts WHERE attempt_id = ?",
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
            SET status = ?, payload_json = ?, error_code = ?, error_message = ?,
                error_class = ?, retryable = ?, backoff_ms = ?, prompt_tokens = ?,
                completion_tokens = ?, total_tokens = ?, cost_usd = ?,
                memory_revision_after = ?, finished_at = ?,
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
            SELECT used_llm_calls, used_total_tokens, reserved_total_tokens, used_total_cost_usd
            FROM conversation_budget_ledger
            WHERE conversation_id = ?
            """,
            (conversation.id,),
        ).fetchone()
        assert ledger_row is not None
        reserved_tokens = int(row["reserved_tokens"])
        next_reserved_total = max(
            0,
            int(ledger_row["reserved_total_tokens"]) - reserved_tokens,
        )
        connection.execute(
            """
            UPDATE conversation_budget_ledger
            SET used_total_tokens = ?,
                reserved_total_tokens = ?,
                used_total_cost_usd = ?,
                updated_at = ?
            WHERE conversation_id = ?
            """,
            (
                int(ledger_row["used_total_tokens"]) + total_tokens,
                next_reserved_total,
                round(float(ledger_row["used_total_cost_usd"]) + total_cost, 6),
                now,
                conversation.id,
            ),
        )
        updated_row = connection.execute(
            "SELECT * FROM llm_attempts WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        assert updated_row is not None
        connection.commit()
    return _attempt_row_to_read(updated_row)


def invoke_model(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    adapter: ModelAdapter,
    request: ModelRequest,
    context: InvocationContext,
) -> dict[str, Any]:
    admitted = admit_attempt(conversation, profile=profile, context=context)
    if not bool(admitted.get("admitted")):
        error = admitted.get("error") or {}
        raise AdmissionRejectedError(
            code=str(error.get("code", "budget_exhausted")),
            message=str(error.get("message", "Conversation LLM budget exhausted.")),
        )
    attempt_id = str(admitted["attempt_id"])
    try:
        response = asyncio.run(adapter.complete(request))
    except Exception as exc:  # pragma: no cover - defensive
        code = getattr(exc, "code", exc.__class__.__name__)
        error_class, retryable, backoff_ms = _classify_model_error(str(code))
        finalize_attempt(
            conversation,
            attempt_id=attempt_id,
            status="failed",
            payload={"exception": str(exc)},
            error_code=str(code),
            error_message=str(exc),
            error_class=error_class,
            retryable=retryable,
            backoff_ms=backoff_ms,
            prompt_tokens=context.estimated_input_tokens,
            completion_tokens=0,
            total_tokens=context.estimated_input_tokens,
        )
        raise InvocationError(
            code=str(code),
            message=str(exc),
            retryable=retryable,
            error_class=error_class,
            backoff_ms=backoff_ms,
        ) from exc
    usage = response.usage
    prompt_tokens = int(usage.get("input_tokens", 0))
    completion_tokens = int(usage.get("output_tokens", 0))
    total_tokens = prompt_tokens + completion_tokens
    try:
        payload = _parse_model_payload(
            response,
            purpose=request.purpose,
        )
    except InvocationError as exc:
        finalize_attempt(
            conversation,
            attempt_id=attempt_id,
            status="failed",
            payload={
                "raw_payload": response.raw_payload,
                "raw_output_text": response.output_text,
                **exc.payload,
            },
            error_code=exc.code,
            error_message=exc.message,
            error_class=exc.error_class,
            retryable=exc.retryable,
            backoff_ms=exc.backoff_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )
        raise InvocationError(
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            error_class=exc.error_class,
            backoff_ms=exc.backoff_ms,
            payload=exc.payload,
            attempt_id=attempt_id,
        ) from exc
    finalize_attempt(
        conversation,
        attempt_id=attempt_id,
        status="succeeded",
        payload=response.raw_payload,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )
    payload["_attempt_id"] = attempt_id
    payload["_raw_output_text"] = response.output_text
    return payload


def _parse_model_payload(response: ModelResponse, *, purpose: str) -> dict[str, Any]:
    try:
        parsed = json.loads(response.output_text)
    except json.JSONDecodeError as exc:
        raise InvocationError(
            code="INVALID_OUTPUT",
            message=f"Invalid structured output for {purpose}.",
            retryable=False,
            error_class="invalid_output",
            payload={"raw_output_text": response.output_text, "error": str(exc)},
        ) from exc
    if not isinstance(parsed, dict):
        raise InvocationError(
            code="INVALID_OUTPUT",
            message=f"Non-object structured output for {purpose}.",
            retryable=False,
            error_class="invalid_output",
            payload={"raw_output_text": response.output_text},
        )
    return parsed


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
    return list_attempt_rows(conversation, run_id=run_id)


def mark_run_attempts_crashed(
    connection: sqlite3.Connection,
    *,
    conversation_id: str,
    run_id: str,
    finished_at: str,
    error_code: str,
    error_message: str,
) -> None:
    rows = connection.execute(
        """
        SELECT attempt_id, reserved_tokens
        FROM llm_attempts
        WHERE run_id = ? AND status = 'started'
        """,
        (run_id,),
    ).fetchall()
    total_released = sum(int(row["reserved_tokens"]) for row in rows)
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
    if total_released:
        connection.execute(
            """
            UPDATE conversation_budget_ledger
            SET reserved_total_tokens = MAX(0, reserved_total_tokens - ?),
                updated_at = ?
            WHERE conversation_id = ?
            """,
            (total_released, finished_at, conversation_id),
        )


def list_admission_rows(
    conversation: ConversationRecord,
    *,
    run_id: str | None = None,
    agent_id: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[object] = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    with connect_transcript(conversation) as connection:
        rows = connection.execute(
            f"""
            SELECT admission_id, owner_kind, owner_id, run_id, agent_id, phase, decision,
                   reason_code, reason_message, reserved_tokens, profile_hash,
                   guardrails_json, payload_json, created_at
            FROM llm_admission_events
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at, admission_id
            """,
            tuple(params),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        items.append(
            {
                "admission_id": row["admission_id"],
                "owner_kind": row["owner_kind"],
                "owner_id": row["owner_id"],
                "run_id": row["run_id"],
                "agent_id": row["agent_id"],
                "phase": row["phase"],
                "decision": row["decision"],
                "reason_code": row["reason_code"],
                "reason_message": row["reason_message"],
                "reserved_tokens": int(row["reserved_tokens"]),
                "profile_hash": row["profile_hash"],
                "guardrails_json": (
                    json.loads(row["guardrails_json"])
                    if row["guardrails_json"]
                    else None
                ),
                "payload_json": (
                    json.loads(row["payload_json"]) if row["payload_json"] else None
                ),
                "created_at": row["created_at"],
            }
        )
    return items


def budget_snapshot(conversation: ConversationRecord) -> dict[str, Any]:
    max_llm_calls, max_total_tokens = _budget_limits(conversation)
    with connect_transcript(conversation) as connection:
        row = connection.execute(
            """
            SELECT used_llm_calls, used_total_tokens, reserved_total_tokens, used_total_cost_usd
            FROM conversation_budget_ledger
            WHERE conversation_id = ?
            """,
            (conversation.id,),
        ).fetchone()
    used_llm_calls = 0 if row is None else int(row["used_llm_calls"])
    used_total_tokens = 0 if row is None else int(row["used_total_tokens"])
    reserved_total_tokens = 0 if row is None else int(row["reserved_total_tokens"])
    used_total_cost = 0.0 if row is None else float(row["used_total_cost_usd"])
    return {
        "max_llm_calls": max_llm_calls,
        "max_total_tokens": max_total_tokens,
        "used_llm_calls": used_llm_calls,
        "used_total_tokens": used_total_tokens,
        "reserved_total_tokens": reserved_total_tokens,
        "used_total_cost_usd": round(used_total_cost, 6),
    }
