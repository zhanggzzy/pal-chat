from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import WebSocket

from pal_chat_server.archive import append_observations, transcript_path
from pal_chat_server.clock import Clock, RealClock
from pal_chat_server.contracts import ModelRequest
from pal_chat_server.db import SQLITE_BUSY_TIMEOUT_MS
from pal_chat_server.errors import AppError
from pal_chat_server.ids import generate_ulid
from pal_chat_server.models import ConversationRecord
from pal_chat_server.schemas import ExperimentProfile
from pal_chat_server.scripted_adapter import ScriptedModelAdapter, ScriptedStep


@dataclass(slots=True)
class MessagePayload:
    client_message_id: str
    content_markdown: str
    mentions: list[str]
    primary_reply_to: str | None
    responds_to: list[str]
    sender_kind: str = "user"
    sender_id: str = "user"
    expected_conversation_seq: int | None = None
    idempotency_key: str | None = None
    causal_episode_id: str | None = None
    caused_by_message_id: str | None = None
    agent_hop: int = 0


@dataclass(slots=True)
class ProjectionStep:
    match_contains: str | None
    operation: str
    segment_title: str | None = None
    updated_summary: str | None = None
    fail: bool = False


class ProjectionError(Exception):
    pass


@dataclass(slots=True)
class PreparedCommit:
    request_hash: str
    checkpoint_seq: int
    checkpoint_revision: int
    checkpoint_snapshot: dict[str, Any]
    created_at: str


@dataclass(slots=True)
class ProjectionResult:
    next_revision: int
    snapshot: dict[str, Any]


class ConversationSocketManager:
    def __init__(self) -> None:
        self._connections: dict[
            str, list[tuple[WebSocket, asyncio.AbstractEventLoop]]
        ] = defaultdict(list)
        self._lock = Lock()

    async def connect(self, conversation_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        loop = asyncio.get_running_loop()
        with self._lock:
            self._connections[conversation_id].append((websocket, loop))

    def disconnect(self, conversation_id: str, websocket: WebSocket) -> None:
        with self._lock:
            current = self._connections.get(conversation_id, [])
            self._connections[conversation_id] = [
                item for item in current if item[0] is not websocket
            ]

    def publish(self, conversation_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            current = list(self._connections.get(conversation_id, []))
        for websocket, loop in current:
            future = asyncio.run_coroutine_threadsafe(websocket.send_json(event), loop)
            future.add_done_callback(
                self._publish_done_callback(conversation_id, websocket)
            )

    def _publish_done_callback(
        self,
        conversation_id: str,
        websocket: WebSocket,
    ) -> Any:
        def callback(future: Future[Any]) -> None:
            self._handle_publish_result(conversation_id, websocket, future)

        return callback

    def _handle_publish_result(
        self,
        conversation_id: str,
        websocket: WebSocket,
        future: Any,
    ) -> None:
        try:
            future.result()
        except Exception:
            self.disconnect(conversation_id, websocket)


SOCKET_MANAGER = ConversationSocketManager()
_CONVERSATION_LOCKS: dict[str, Lock] = defaultdict(Lock)
_OUTBOX_SIGNAL_HANDLERS: list[Callable[[str], None]] = []


def utc_now(clock: Clock | None = None) -> datetime:
    return (clock or RealClock()).now_utc()


def register_outbox_signal(handler: Callable[[str], None]) -> None:
    if handler not in _OUTBOX_SIGNAL_HANDLERS:
        _OUTBOX_SIGNAL_HANDLERS.append(handler)


def unregister_outbox_signal(handler: Callable[[str], None]) -> None:
    if handler in _OUTBOX_SIGNAL_HANDLERS:
        _OUTBOX_SIGNAL_HANDLERS.remove(handler)


def signal_outbox_dispatch(conversation_id: str) -> None:
    for handler in _OUTBOX_SIGNAL_HANDLERS:
        handler(conversation_id)


def transcript_db_path(conversation: ConversationRecord) -> Path:
    if conversation.archive_dir is None:
        raise AppError(
            code="archive_missing",
            status_code=409,
            message="Conversation archive is missing.",
        )
    return transcript_path(Path(conversation.archive_dir))


def message_request_hash(payload: MessagePayload) -> str:
    canonical = json.dumps(
        {
            "client_message_id": payload.client_message_id,
            "content_markdown": payload.content_markdown,
            "mentions": payload.mentions,
            "primary_reply_to": payload.primary_reply_to,
            "responds_to": payload.responds_to,
            "sender_kind": payload.sender_kind,
            "sender_id": payload.sender_id,
            "idempotency_key": payload.idempotency_key,
            "causal_episode_id": payload.causal_episode_id,
            "caused_by_message_id": payload.caused_by_message_id,
            "agent_hop": payload.agent_hop,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def connect_transcript(conversation: ConversationRecord) -> sqlite3.Connection:
    connection = sqlite3.connect(transcript_db_path(conversation))
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=FULL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
    connection.row_factory = sqlite3.Row
    return connection


def allowed_member_ids(profile: ExperimentProfile) -> set[str]:
    return {"user", profile.agent_a.agent_id, profile.agent_b.agent_id}


def fetch_latest_cp_snapshot(connection: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
    row = connection.execute(
        """
        SELECT projection_revision, snapshot_json
        FROM cp_revisions
        ORDER BY projection_revision DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return 0, {"segments": []}
    snapshot = json.loads(row["snapshot_json"])
    return int(row["projection_revision"]), snapshot


def parse_projection_steps(profile: ExperimentProfile) -> list[ProjectionStep]:
    selection = profile.modules.get("projection")
    if selection is None:
        return []
    steps = selection.config.get("script", [])
    return [ProjectionStep(**item) for item in steps]


def projection_script_steps(profile: ExperimentProfile) -> list[ScriptedStep]:
    selection = profile.modules.get("projection")
    if selection is None:
        return []
    items = selection.config.get("script", [])
    steps: list[ScriptedStep] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "output_json" in item or "output_text" in item or "fail_code" in item:
            step_payload = dict(item)
            step_payload.setdefault("purpose", "projection")
            steps.append(ScriptedStep.model_validate(step_payload))
            continue
        legacy = ProjectionStep(**item)
        if legacy.fail:
            steps.append(
                ScriptedStep(
                    purpose="projection",
                    match_contains=legacy.match_contains,
                    fail_code="PROJECTION_FAILED",
                )
            )
            continue
        steps.append(
            ScriptedStep(
                purpose="projection",
                match_contains=legacy.match_contains,
                output_json={
                    "operation": legacy.operation,
                    "segment_title": legacy.segment_title,
                    "updated_summary": legacy.updated_summary,
                },
            )
        )
    return steps


def budget_limits(profile: ExperimentProfile) -> tuple[int, int, int]:
    selection = profile.modules.get("budget")
    config = selection.config if selection is not None else {}
    return (
        int(config.get("max_agent_actions_per_episode", 4)),
        int(config.get("max_actions_per_agent_per_episode", 2)),
        int(config.get("max_consecutive_agent_hops", 2)),
    )


def _apply_projection_operation(
    *,
    strategy_id: str,
    content_markdown: str,
    primary_reply_to: str | None,
    mentions: list[str],
    current_revision: int,
    current_snapshot: dict[str, Any],
    operation: str,
    segment_title: str | None,
    updated_summary: str | None,
) -> ProjectionResult:
    snapshot = json.loads(json.dumps(current_snapshot))
    segments = snapshot.setdefault("segments", [])

    next_revision = current_revision + 1
    default_title = mentions[0] if mentions else "公共消息"
    resolved_title = segment_title or default_title

    if operation == "START_NEW_SEGMENT" or not segments:
        if segments:
            segments[-1]["status"] = "closed"
            segments[-1]["closed_revision"] = next_revision
        segment = {
            "segment_id": generate_ulid(),
            "ordinal": len(segments) + 1,
            "status": "open",
            "message_refs": [],
            "start_seq": None,
            "end_seq": None,
            "title": resolved_title,
            "summary": updated_summary or content_markdown[:140],
            "base_activation": 0.5 if primary_reply_to else 0.35,
            "activation_updated_at": utc_now().isoformat(),
            "created_revision": next_revision,
            "closed_revision": None,
            "projection_trace_ref": f"trace://projection/{next_revision}",
        }
        segments.append(segment)
    elif operation != "APPEND_CURRENT":
        raise ProjectionError(f"Unsupported projection operation: {operation}")

    snapshot["last_operation"] = operation
    if updated_summary is not None and segments:
        segments[-1]["summary"] = updated_summary
    return ProjectionResult(next_revision=next_revision, snapshot=snapshot)


def _fixed_window_projection(
    *,
    profile: ExperimentProfile,
    content_markdown: str,
    primary_reply_to: str | None,
    mentions: list[str],
    current_revision: int,
    current_snapshot: dict[str, Any],
) -> ProjectionResult:
    selection = profile.modules["projection"]
    snapshot = json.loads(json.dumps(current_snapshot))
    segments = snapshot.setdefault("segments", [])
    window_size = int(selection.config.get("window_size", 2))
    if segments and len(segments[-1]["message_refs"]) < window_size:
        operation = "APPEND_CURRENT"
    else:
        operation = "START_NEW_SEGMENT"
    return _apply_projection_operation(
        strategy_id=selection.module_id,
        content_markdown=content_markdown,
        primary_reply_to=primary_reply_to,
        mentions=mentions,
        current_revision=current_revision,
        current_snapshot=current_snapshot,
        operation=operation,
        segment_title=None,
        updated_summary=content_markdown[:140],
    )


def apply_projection(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    payload: MessagePayload,
    current_revision: int,
    current_snapshot: dict[str, Any],
) -> ProjectionResult:
    selection = profile.modules["projection"]
    if selection.module_id == "projection.fixed-window":
        return _fixed_window_projection(
            profile=profile,
            content_markdown=payload.content_markdown,
            primary_reply_to=payload.primary_reply_to,
            mentions=payload.mentions,
            current_revision=current_revision,
            current_snapshot=current_snapshot,
        )

    adapter = ScriptedModelAdapter(projection_script_steps(profile))
    request = ModelRequest(
        purpose="projection",
        system_prompt="",
        messages=[{"content": payload.content_markdown}],
        metadata={
            "has_open_segment": bool(current_snapshot.get("segments")),
            "default_segment_title": payload.mentions[0] if payload.mentions else "公共消息",
            "primary_reply_to": payload.primary_reply_to,
            "responds_to": payload.responds_to,
            "sender_kind": payload.sender_kind,
            "sender_id": payload.sender_id,
        },
    )
    from pal_chat_server.attempts import (
        AdmissionRejectedError,
        InvocationContext,
        InvocationError,
        invoke_model,
    )

    try:
        projection_payload = invoke_model(
            conversation,
            profile=profile,
            adapter=adapter,
            request=request,
            context=InvocationContext(
                owner_kind="server_projection",
                owner_id=payload.client_message_id,
                phase="projection",
                profile_hash=conversation.profile_hash or "",
                payload={
                    "client_message_id": payload.client_message_id,
                    "expected_conversation_seq": payload.expected_conversation_seq,
                },
                estimated_input_tokens=max(1, len(payload.content_markdown) // 4),
                max_output_tokens=int(request.max_tokens),
            ),
        )
    except AdmissionRejectedError:
        return _apply_projection_operation(
            strategy_id=selection.module_id,
            content_markdown=payload.content_markdown,
            primary_reply_to=payload.primary_reply_to,
            mentions=payload.mentions,
            current_revision=current_revision,
            current_snapshot=current_snapshot,
            operation="APPEND_CURRENT",
            segment_title=None,
            updated_summary=payload.content_markdown[:140],
        )
    except InvocationError as exc:
        raise ProjectionError(exc.message) from exc

    operation = str(projection_payload.get("operation", "APPEND_CURRENT"))
    return _apply_projection_operation(
        strategy_id=selection.module_id,
        content_markdown=payload.content_markdown,
        primary_reply_to=payload.primary_reply_to,
        mentions=payload.mentions,
        current_revision=current_revision,
        current_snapshot=current_snapshot,
        operation=operation,
        segment_title=(
            str(projection_payload["segment_title"])
            if projection_payload.get("segment_title") is not None
            else None
        ),
        updated_summary=(
            str(projection_payload["updated_summary"])
            if projection_payload.get("updated_summary") is not None
            else payload.content_markdown[:140]
        ),
    )


def _prepare_commit(
    connection: sqlite3.Connection,
    *,
    conversation: ConversationRecord,
    profile: ExperimentProfile,
    payload: MessagePayload,
    clock: Clock | None,
) -> tuple[dict[str, Any], dict[str, Any], PreparedCommit] | PreparedCommit:
    request_hash = message_request_hash(payload)
    now = utc_now(clock).isoformat()
    current_seq_row = connection.execute(
        "SELECT COALESCE(MAX(conversation_seq), 0) AS max_seq FROM messages"
    ).fetchone()
    current_seq = int(current_seq_row["max_seq"])
    if (
        payload.expected_conversation_seq is not None
        and payload.expected_conversation_seq != current_seq
    ):
        raise AppError(
            code="stale_sequence",
            status_code=409,
            message="Conversation sequence is stale.",
            details={
                "expected_conversation_seq": payload.expected_conversation_seq,
                "actual_conversation_seq": current_seq,
            },
        )
    existing_submission = connection.execute(
        """
        SELECT *
        FROM submissions
        WHERE client_message_id = ?
        """,
        (payload.client_message_id,),
    ).fetchone()
    if existing_submission is not None:
        if existing_submission["request_hash"] != request_hash:
            raise AppError(
                code="idempotency_conflict",
                status_code=409,
                message="client_message_id already used with different payload.",
            )
        if existing_submission["status"] == "committed":
            message_row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (existing_submission["message_id"],),
            ).fetchone()
            assert message_row is not None
            message_id = str(message_row["message_id"])
            mentions_by_message, responds_to_by_message = _grouped_message_metadata(
                connection,
                [message_id],
            )
            message = build_message_record(
                message_row,
                mentions=mentions_by_message.get(message_id, []),
                responds_to=responds_to_by_message.get(message_id, []),
            )
            cp_revision = get_cp_revision(conversation, message["cp_revision"])
            return message, cp_revision, PreparedCommit(
                request_hash=request_hash,
                checkpoint_seq=current_seq,
                checkpoint_revision=cp_revision["projection_revision"],
                checkpoint_snapshot=cp_revision["snapshot"],
                created_at=now,
            )
    validate_message_payload(payload, profile=profile, connection=connection)
    enforce_episode_budget(
        connection=connection,
        profile=profile,
        payload=payload,
        now=now,
    )
    connection.execute(
        """
        INSERT INTO submissions(
          client_message_id, request_hash, content_markdown, mentions_json,
          primary_reply_to, responds_to_json, status, error_code, error_message,
          message_id, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_message_id) DO UPDATE SET
          request_hash=excluded.request_hash,
          content_markdown=excluded.content_markdown,
          mentions_json=excluded.mentions_json,
          primary_reply_to=excluded.primary_reply_to,
          responds_to_json=excluded.responds_to_json,
          status=excluded.status,
          error_code=excluded.error_code,
          error_message=excluded.error_message,
          updated_at=excluded.updated_at
        """,
        (
            payload.client_message_id,
            request_hash,
            payload.content_markdown,
            json.dumps(payload.mentions),
            payload.primary_reply_to,
            json.dumps(payload.responds_to),
            "pending",
            None,
            None,
            None,
            now,
            now,
        ),
    )
    current_revision, current_snapshot = fetch_latest_cp_snapshot(connection)
    return PreparedCommit(
        request_hash=request_hash,
        checkpoint_seq=current_seq,
        checkpoint_revision=current_revision,
        checkpoint_snapshot=current_snapshot,
        created_at=now,
    )


def update_segment_with_message(
    snapshot: dict[str, Any],
    *,
    message_id: str,
    conversation_seq: int,
    content_markdown: str,
) -> dict[str, Any]:
    segments = snapshot["segments"]
    segment = segments[-1]
    segment["message_refs"].append(message_id)
    if segment["start_seq"] is None:
        segment["start_seq"] = conversation_seq
    segment["end_seq"] = conversation_seq
    segment["summary"] = content_markdown[:140]
    return snapshot


def _grouped_message_metadata(
    connection: sqlite3.Connection,
    message_ids: list[str],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    if not message_ids:
        return {}, {}
    placeholders = ",".join("?" for _ in message_ids)
    mentions_by_message: dict[str, list[str]] = {message_id: [] for message_id in message_ids}
    responds_to_by_message: dict[str, list[str]] = {
        message_id: [] for message_id in message_ids
    }
    for row in connection.execute(
        f"""
        SELECT message_id, member_id
        FROM message_mentions
        WHERE message_id IN ({placeholders})
        ORDER BY message_id, member_id
        """,
        message_ids,
    ).fetchall():
        mentions_by_message[str(row["message_id"])].append(str(row["member_id"]))
    for row in connection.execute(
        f"""
        SELECT message_id, responds_to_message_id
        FROM message_response_refs
        WHERE message_id IN ({placeholders})
        ORDER BY message_id, ordinal
        """,
        message_ids,
    ).fetchall():
        responds_to_by_message[str(row["message_id"])].append(
            str(row["responds_to_message_id"])
        )
    return mentions_by_message, responds_to_by_message


def build_message_record(
    row: sqlite3.Row,
    *,
    mentions: list[str],
    responds_to: list[str],
) -> dict[str, Any]:
    return {
        "message_id": row["message_id"],
        "conversation_seq": int(row["conversation_seq"]),
        "sender_kind": row["sender_kind"],
        "sender_id": row["sender_id"],
        "content_markdown": row["content_markdown"],
        "mentions": list(mentions),
        "primary_reply_to": row["primary_reply_to"],
        "responds_to": list(responds_to),
        "client_message_id": row["client_message_id"],
        "causal_episode_id": row["causal_episode_id"],
        "caused_by_message_id": row["caused_by_message_id"],
        "agent_hop": int(row["agent_hop"]),
        "committed_at": row["committed_at"],
        "cp_revision": int(row["cp_revision"]),
    }


def list_messages(
    conversation: ConversationRecord,
    *,
    after_seq: int,
    limit: int,
) -> list[dict[str, Any]]:
    with connect_transcript(conversation) as connection:
        return list_messages_from_connection(
            connection,
            after_seq=after_seq,
            limit=limit,
        )


def list_messages_from_connection(
    connection: sqlite3.Connection,
    *,
    after_seq: int,
    limit: int,
) -> list[dict[str, Any]]:
    if after_seq > 0:
        rows = connection.execute(
            """
            SELECT *
            FROM messages
            WHERE conversation_seq > ?
              AND (
                sender_kind != 'agent'
                OR EXISTS (
                  SELECT 1
                  FROM agent_runs
                  WHERE agent_runs.run_id = messages.client_message_id
                    AND agent_runs.status = 'COMMITTED'
                )
              )
            ORDER BY conversation_seq
            LIMIT ?
            """,
            (after_seq, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT *
            FROM (
              SELECT *
              FROM messages
              WHERE (
                sender_kind != 'agent'
                OR EXISTS (
                  SELECT 1
                  FROM agent_runs
                  WHERE agent_runs.run_id = messages.client_message_id
                    AND agent_runs.status = 'COMMITTED'
                )
              )
              ORDER BY conversation_seq DESC
              LIMIT ?
            ) recent_messages
            ORDER BY conversation_seq
            """,
            (limit,),
        ).fetchall()
    message_ids = [str(row["message_id"]) for row in rows]
    mentions_by_message, responds_to_by_message = _grouped_message_metadata(
        connection, message_ids
    )
    return [
        build_message_record(
            row,
            mentions=mentions_by_message.get(str(row["message_id"]), []),
            responds_to=responds_to_by_message.get(str(row["message_id"]), []),
        )
        for row in rows
    ]


def list_cp_revisions(conversation: ConversationRecord) -> list[dict[str, Any]]:
    with connect_transcript(conversation) as connection:
        rows = connection.execute(
            """
            SELECT projection_revision, covered_through_seq, strategy_id, strategy_version,
                   snapshot_json, trace_ref, created_at
            FROM cp_revisions
            ORDER BY projection_revision
            """
        ).fetchall()
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


def get_cp_revision(conversation: ConversationRecord, projection_revision: int) -> dict[str, Any]:
    with connect_transcript(conversation) as connection:
        row = connection.execute(
            """
            SELECT projection_revision, covered_through_seq, strategy_id, strategy_version,
                   snapshot_json, trace_ref, created_at
            FROM cp_revisions
            WHERE projection_revision = ?
            """,
            (projection_revision,),
        ).fetchone()
    if row is None:
        raise AppError(
            code="cp_revision_not_found",
            status_code=404,
            message="CP revision not found.",
        )
    return {
        "projection_revision": int(row["projection_revision"]),
        "covered_through_seq": int(row["covered_through_seq"]),
        "strategy_id": row["strategy_id"],
        "strategy_version": row["strategy_version"],
        "snapshot": json.loads(row["snapshot_json"]),
        "trace_ref": row["trace_ref"],
        "created_at": row["created_at"],
    }


def replay_events(conversation: ConversationRecord, *, after_seq: int) -> list[dict[str, Any]]:
    with connect_transcript(conversation) as connection:
        rows = connection.execute(
            """
            SELECT rowid, event_id, event_type, conversation_seq, payload_json, created_at
            FROM outbox_events
            WHERE conversation_seq IS NOT NULL AND conversation_seq > ?
            ORDER BY conversation_seq, rowid
            """,
            (after_seq,),
        ).fetchall()
    return [
        {
            "protocol_version": 1,
            "event_id": row["event_id"],
            "event_type": row["event_type"],
            "conversation_id": conversation.id,
            "emitted_at": row["created_at"],
            "conversation_seq": row["conversation_seq"],
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]


def session_snapshot(conversation: ConversationRecord) -> dict[str, Any]:
    with connect_transcript(conversation) as connection:
        latest_message = connection.execute(
            "SELECT COALESCE(MAX(conversation_seq), 0) AS max_seq FROM messages"
        ).fetchone()
        current_revision, snapshot = fetch_latest_cp_snapshot(connection)
    return {
        "protocol_version": 1,
        "event_type": "session.snapshot",
        "conversation_id": conversation.id,
        "payload": {
            "latest_conversation_seq": int(latest_message["max_seq"]),
            "latest_cp_revision": current_revision,
            "cp_snapshot": snapshot,
        },
    }


def validate_message_payload(
    payload: MessagePayload,
    *,
    profile: ExperimentProfile,
    connection: sqlite3.Connection,
) -> None:
    member_ids = allowed_member_ids(profile)
    unknown_mentions = [item for item in payload.mentions if item not in member_ids]
    if unknown_mentions:
        raise AppError(
            code="unknown_mention",
            status_code=422,
            message="Unknown mention target.",
            details={"mentions": unknown_mentions},
        )
    refs = [item for item in payload.responds_to if item]
    if payload.primary_reply_to:
        refs.append(payload.primary_reply_to)
    for message_id in refs:
        row = connection.execute(
            "SELECT message_id FROM messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise AppError(
                code="reply_target_not_found",
                status_code=422,
                message="Reply target was not found.",
                details={"message_id": message_id},
            )


def enforce_episode_budget(
    *,
    connection: sqlite3.Connection,
    profile: ExperimentProfile,
    payload: MessagePayload,
    now: str,
) -> None:
    if payload.sender_kind != "agent" or payload.causal_episode_id is None:
        return
    max_actions, max_actions_per_agent, max_hops = budget_limits(profile)
    if payload.agent_hop > max_hops:
        raise AppError(
            code="budget_exhausted",
            status_code=409,
            message="Agent hop budget exhausted.",
        )
    row = connection.execute(
        """
        SELECT total_actions, agent_a_actions, agent_b_actions, max_agent_hop
        FROM causal_episode_budget
        WHERE episode_id = ?
        """,
        (payload.causal_episode_id,),
    ).fetchone()
    total_actions = int(row["total_actions"]) if row is not None else 0
    agent_a_actions = int(row["agent_a_actions"]) if row is not None else 0
    agent_b_actions = int(row["agent_b_actions"]) if row is not None else 0
    if total_actions + 1 > max_actions:
        raise AppError(
            code="budget_exhausted",
            status_code=409,
            message="Episode action budget exhausted.",
        )
    if payload.sender_id == profile.agent_a.agent_id:
        if agent_a_actions + 1 > max_actions_per_agent:
            raise AppError(
                code="budget_exhausted",
                status_code=409,
                message="Agent action budget exhausted.",
            )
    elif payload.sender_id == profile.agent_b.agent_id:
        if agent_b_actions + 1 > max_actions_per_agent:
            raise AppError(
                code="budget_exhausted",
                status_code=409,
                message="Agent action budget exhausted.",
            )
    else:
        raise AppError(
            code="unknown_agent",
            status_code=409,
            message="Unknown agent sender.",
        )


def record_episode_budget(
    *,
    connection: sqlite3.Connection,
    profile: ExperimentProfile,
    payload: MessagePayload,
    now: str,
) -> None:
    if payload.sender_kind != "agent" or payload.causal_episode_id is None:
        return
    row = connection.execute(
        """
        SELECT total_actions, agent_a_actions, agent_b_actions, max_agent_hop
        FROM causal_episode_budget
        WHERE episode_id = ?
        """,
        (payload.causal_episode_id,),
    ).fetchone()
    total_actions = int(row["total_actions"]) if row is not None else 0
    agent_a_actions = int(row["agent_a_actions"]) if row is not None else 0
    agent_b_actions = int(row["agent_b_actions"]) if row is not None else 0
    max_agent_hop = int(row["max_agent_hop"]) if row is not None else 0
    if payload.sender_id == profile.agent_a.agent_id:
        agent_a_actions += 1
    elif payload.sender_id == profile.agent_b.agent_id:
        agent_b_actions += 1
    connection.execute(
        """
        INSERT INTO causal_episode_budget(
          episode_id, root_message_ids_json, total_actions, agent_a_actions,
          agent_b_actions, max_agent_hop, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(episode_id) DO UPDATE SET
          total_actions = excluded.total_actions,
          agent_a_actions = excluded.agent_a_actions,
          agent_b_actions = excluded.agent_b_actions,
          max_agent_hop = excluded.max_agent_hop,
          updated_at = excluded.updated_at
        """,
        (
            payload.causal_episode_id,
            json.dumps([], ensure_ascii=False),
            total_actions + 1,
            agent_a_actions,
            agent_b_actions,
            max(max_agent_hop, payload.agent_hop),
            now,
        ),
    )


def commit_message(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    payload: MessagePayload,
    clock: Clock | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    lock = _CONVERSATION_LOCKS[conversation.id]
    while True:
        with lock, connect_transcript(conversation) as connection:
            connection.execute("BEGIN IMMEDIATE")
            prepared = _prepare_commit(
                connection,
                conversation=conversation,
                profile=profile,
                payload=payload,
                clock=clock,
            )
            if isinstance(prepared, tuple):
                connection.commit()
                return prepared[0], prepared[1], []
            connection.commit()

        try:
            projection_result = apply_projection(
                conversation,
                profile=profile,
                payload=payload,
                current_revision=prepared.checkpoint_revision,
                current_snapshot=prepared.checkpoint_snapshot,
            )
        except ProjectionError as exc:
            with lock, connect_transcript(conversation) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE submissions
                    SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                    WHERE client_message_id = ?
                    """,
                    (
                        "failed",
                        "CP_FAILED",
                        str(exc),
                        prepared.created_at,
                        payload.client_message_id,
                    ),
                )
                connection.commit()
            raise AppError(
                code="projection_failed",
                status_code=409,
                message="Projection update failed.",
                details={"reason": str(exc)},
            ) from exc

        with lock, connect_transcript(conversation) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current_seq_row = connection.execute(
                "SELECT COALESCE(MAX(conversation_seq), 0) AS max_seq FROM messages"
            ).fetchone()
            current_seq = int(current_seq_row["max_seq"])
            current_revision, _ = fetch_latest_cp_snapshot(connection)
            if (
                current_seq != prepared.checkpoint_seq
                or current_revision != prepared.checkpoint_revision
            ):
                if payload.expected_conversation_seq is None:
                    connection.commit()
                    continue
                connection.execute(
                    """
                    UPDATE submissions
                    SET status = ?, error_code = ?, error_message = ?, updated_at = ?
                    WHERE client_message_id = ?
                    """,
                    (
                        "failed",
                        "CP_STALE",
                        "Projection checkpoint became stale before commit.",
                        prepared.created_at,
                        payload.client_message_id,
                    ),
                )
                connection.commit()
                raise AppError(
                    code="projection_stale",
                    status_code=409,
                    message="Projection checkpoint became stale before commit.",
                    details={
                        "checkpoint_seq": prepared.checkpoint_seq,
                        "current_seq": current_seq,
                        "checkpoint_revision": prepared.checkpoint_revision,
                        "current_revision": current_revision,
                    },
                )

            now = prepared.created_at
            next_seq = current_seq + 1
            message_id = generate_ulid()
            snapshot = update_segment_with_message(
                projection_result.snapshot,
                message_id=message_id,
                conversation_seq=next_seq,
                content_markdown=payload.content_markdown,
            )
            content_hash = sha256(payload.content_markdown.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO messages(
                  message_id, conversation_seq, sender_kind, sender_id, content_markdown,
                  primary_reply_to, causal_episode_id, caused_by_message_id, agent_hop,
                  client_message_id, idempotency_key, server_received_at, committed_at,
                  cp_revision, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    next_seq,
                    payload.sender_kind,
                    payload.sender_id,
                    payload.content_markdown,
                    payload.primary_reply_to,
                    payload.causal_episode_id,
                    payload.caused_by_message_id or payload.primary_reply_to,
                    payload.agent_hop,
                    payload.client_message_id,
                    payload.idempotency_key or payload.client_message_id,
                    now,
                    now,
                    projection_result.next_revision,
                    content_hash,
                ),
            )
            for mention in payload.mentions:
                connection.execute(
                    """
                    INSERT INTO message_mentions(message_id, member_id)
                    VALUES (?, ?)
                    """,
                    (message_id, mention),
                )
            for ordinal, reply_to in enumerate(payload.responds_to, start=1):
                connection.execute(
                    """
                    INSERT INTO message_response_refs(message_id, responds_to_message_id, ordinal)
                    VALUES (?, ?, ?)
                    """,
                    (message_id, reply_to, ordinal),
                )
            connection.execute(
                """
                INSERT INTO cp_revisions(
                  projection_revision, covered_through_seq, strategy_id, strategy_version,
                  snapshot_json, trace_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    projection_result.next_revision,
                    next_seq,
                    profile.modules["projection"].module_id,
                    "1.0.0",
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    f"trace://projection/{projection_result.next_revision}",
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE submissions
                SET status = ?, error_code = NULL, error_message = NULL,
                    message_id = ?, updated_at = ?
                WHERE client_message_id = ?
                """,
                ("committed", message_id, now, payload.client_message_id),
            )
            record_episode_budget(
                connection=connection,
                profile=profile,
                payload=payload,
                now=now,
            )
            outbox_events = [
                (
                    generate_ulid(),
                    "submission.status_changed",
                    None,
                    {
                        "client_message_id": payload.client_message_id,
                        "status": "committed",
                        "message_id": message_id,
                    },
                ),
                (
                    generate_ulid(),
                    "message.committed",
                    next_seq,
                    {
                        "message": {
                            "message_id": message_id,
                            "conversation_seq": next_seq,
                            "sender_kind": payload.sender_kind,
                            "sender_id": payload.sender_id,
                            "content_markdown": payload.content_markdown,
                            "mentions": payload.mentions,
                            "primary_reply_to": payload.primary_reply_to,
                            "responds_to": payload.responds_to,
                            "client_message_id": payload.client_message_id,
                            "causal_episode_id": payload.causal_episode_id,
                            "caused_by_message_id": payload.caused_by_message_id,
                            "agent_hop": payload.agent_hop,
                            "committed_at": now,
                            "cp_revision": projection_result.next_revision,
                        }
                    },
                ),
                (
                    generate_ulid(),
                    "cp.revision_committed",
                    next_seq,
                    {
                        "cp_revision": {
                            "projection_revision": projection_result.next_revision,
                            "covered_through_seq": next_seq,
                            "strategy_id": profile.modules["projection"].module_id,
                            "strategy_version": "1.0.0",
                            "snapshot": snapshot,
                            "created_at": now,
                        }
                    },
                ),
            ]
            for event_id, event_type, conversation_seq, event_payload in outbox_events:
                connection.execute(
                    """
                    INSERT INTO outbox_events(
                      event_id, event_type, conversation_seq, payload_json, status,
                      dispatch_attempts, dispatched_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        conversation_seq,
                        json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
                        "pending",
                        0,
                        None,
                        now,
                    ),
                )
            message_row = connection.execute(
                "SELECT * FROM messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
            assert message_row is not None
            message = build_message_record(
                message_row,
                mentions=payload.mentions,
                responds_to=payload.responds_to,
            )
            cp_revision = {
                "projection_revision": projection_result.next_revision,
                "covered_through_seq": next_seq,
                "strategy_id": profile.modules["projection"].module_id,
                "strategy_version": "1.0.0",
                "snapshot": snapshot,
                "trace_ref": f"trace://projection/{projection_result.next_revision}",
                "created_at": now,
            }
            connection.commit()
        signal_outbox_dispatch(conversation.id)
        return message, cp_revision, []


def retry_submission(
    conversation: ConversationRecord,
    *,
    profile: ExperimentProfile,
    client_message_id: str,
    clock: Clock | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    with connect_transcript(conversation) as connection:
        row = connection.execute(
            "SELECT * FROM submissions WHERE client_message_id = ?",
            (client_message_id,),
        ).fetchone()
    if row is None:
        raise AppError(
            code="submission_not_found",
            status_code=404,
            message="Submission not found.",
        )
    payload = MessagePayload(
        client_message_id=client_message_id,
        content_markdown=row["content_markdown"],
        mentions=json.loads(row["mentions_json"]),
        primary_reply_to=row["primary_reply_to"],
        responds_to=json.loads(row["responds_to_json"]),
    )
    return commit_message(
        conversation,
        profile=profile,
        payload=payload,
        clock=clock,
    )


def dispatch_pending_outbox(
    conversation: ConversationRecord,
    *,
    clock: Clock | None = None,
) -> list[dict[str, Any]]:
    emitted: list[dict[str, Any]] = []
    now = utc_now(clock).isoformat()
    root = Path(conversation.archive_dir or "")
    observations: list[dict[str, Any]] = []
    with connect_transcript(conversation) as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT rowid, event_id, event_type, conversation_seq, payload_json, created_at
            FROM outbox_events
            WHERE dispatched_at IS NULL
            ORDER BY rowid
            """
        ).fetchall()
        for row in rows:
            event = {
                "protocol_version": 1,
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "conversation_id": conversation.id,
                "emitted_at": row["created_at"],
                "conversation_seq": row["conversation_seq"],
                "payload": json.loads(row["payload_json"]),
            }
            emitted.append(event)
            SOCKET_MANAGER.publish(conversation.id, event)
            if root:
                observations.append(
                    {
                        "archive_schema_version": 1,
                        "record_id": generate_ulid(),
                        "experiment_id": conversation.id,
                        "observed_sequence": row["conversation_seq"] or 0,
                        "observed_at_utc": now,
                        "observed_at_monotonic_ns": 0,
                        "source": "server",
                        "destination": "browser",
                        "direction": "out",
                        "content_type": "application/json",
                        "payload_encoding": "json",
                        "payload": {
                            "event_id": row["event_id"],
                            "event_type": row["event_type"],
                        },
                        "checksum": f"sha256:{row['event_id']}",
                    }
                )
            connection.execute(
                """
                UPDATE outbox_events
                SET status = ?, dispatch_attempts = dispatch_attempts + 1, dispatched_at = ?
                WHERE event_id = ? AND dispatched_at IS NULL
                """,
                ("dispatched", now, row["event_id"]),
            )
        connection.commit()
    if root:
        append_observations(root / "observations.ndjson", observations)
    return emitted
