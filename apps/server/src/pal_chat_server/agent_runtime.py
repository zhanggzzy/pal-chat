from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from hashlib import sha256
from typing import Any, cast

from pal_chat_server.clock import Clock, RealClock, VirtualClock
from pal_chat_server.errors import AppError
from pal_chat_server.ids import generate_ulid
from pal_chat_server.models import ConversationRecord, ConversationStatus
from pal_chat_server.schemas import ExperimentProfile
from pal_chat_server.scripted_adapter import ScriptedModelAdapter, ScriptedStep
from pal_chat_server.sequence_runtime import (
    MessagePayload,
    commit_message,
    connect_transcript,
    dispatch_pending_outbox,
    list_messages,
    utc_now,
)


def _json_list(value: list[str]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _is_all_mention(profile: ExperimentProfile, mentions: list[str]) -> bool:
    return {
        profile.agent_a.agent_id,
        profile.agent_b.agent_id,
    }.issubset(set(mentions))


def _episode_id(root_message_ids: list[str]) -> str:
    joined = "|".join(root_message_ids)
    return f"episode-{sha256(joined.encode('utf-8')).hexdigest()[:16]}"


def _timing_wait_ms(profile: ExperimentProfile, *, direct_mention: bool, content: str) -> int:
    selection = profile.modules.get("timing")
    config = selection.config if selection is not None else {}
    base_ms = int(config.get("base_wait_ms", 0))
    per_char_ms = int(config.get("per_char_wait_ms", 0))
    minimum_typing_ms = int(config.get("minimum_typing_ms", 0))
    if direct_mention:
        return max(minimum_typing_ms, base_ms + len(content) * per_char_ms)
    return max(base_ms, len(content) * per_char_ms)


def _runtime_enabled(profile: ExperimentProfile) -> bool:
    return bool(profile.metadata.get("agent_runtime_enabled")) or int(
        profile.metadata.get("phase", 1)
    ) >= 3


def _scripted_steps(profile: ExperimentProfile) -> list[ScriptedStep]:
    selection = profile.modules["model_adapter"]
    return [ScriptedStep.model_validate(item) for item in selection.config.get("script", [])]


@dataclass(slots=True)
class PendingCall:
    purpose: str
    selected_step: ScriptedStep
    ready_at: Any


@dataclass(slots=True)
class WorkerState:
    agent_id: str
    worker_state: str
    reliable_seq: int
    dirty_since_seq: int | None
    pending_message_ids: list[str] = field(default_factory=list)
    pending_root_message_ids: list[str] = field(default_factory=list)
    active_run_id: str | None = None
    typing_status: str = "idle"
    restart_count: int = 0
    profile_hash: str = ""
    current_observation_ids: list[str] = field(default_factory=list)
    current_root_message_ids: list[str] = field(default_factory=list)
    current_expected_seq: int | None = None
    current_causal_episode_id: str | None = None
    current_caused_by_message_id: str | None = None
    current_agent_hop: int = 0
    current_must_reply: bool = False
    current_all_mention: bool = False
    current_direct_mention: bool = False
    current_decision: dict[str, Any] | None = None
    current_draft: dict[str, Any] | None = None
    earliest_send_at: Any | None = None
    pending_call: PendingCall | None = None


@dataclass(slots=True)
class ConversationRuntime:
    conversation_id: str
    profile_hash: str
    workers: dict[str, WorkerState]
    adapters: dict[str, ScriptedModelAdapter]
    paused: bool = False


class AgentRuntimeManager:
    def __init__(self, *, clock: Clock | None = None, auto_pump: bool = True) -> None:
        self.clock = clock or RealClock()
        self.auto_pump = auto_pump
        self._runtimes: dict[str, ConversationRuntime] = {}

    def register_conversation(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
    ) -> None:
        if not _runtime_enabled(profile):
            return
        if conversation.profile_hash is None:
            raise AppError(
                code="profile_hash_missing",
                status_code=409,
                message="Conversation profile hash is missing.",
            )
        runtime = self._runtimes.get(conversation.id)
        if runtime is None or runtime.profile_hash != conversation.profile_hash:
            steps = _scripted_steps(profile)
            runtime = ConversationRuntime(
                conversation_id=conversation.id,
                profile_hash=conversation.profile_hash,
                workers={
                    profile.agent_a.agent_id: WorkerState(
                        agent_id=profile.agent_a.agent_id,
                        worker_state="LISTENING",
                        reliable_seq=0,
                        dirty_since_seq=None,
                        profile_hash=conversation.profile_hash,
                    ),
                    profile.agent_b.agent_id: WorkerState(
                        agent_id=profile.agent_b.agent_id,
                        worker_state="LISTENING",
                        reliable_seq=0,
                        dirty_since_seq=None,
                        profile_hash=conversation.profile_hash,
                    ),
                },
                adapters={
                    profile.agent_a.agent_id: ScriptedModelAdapter(steps, clock=self.clock),
                    profile.agent_b.agent_id: ScriptedModelAdapter(steps, clock=self.clock),
                },
            )
            self._runtimes[conversation.id] = runtime
        with connect_transcript(conversation) as connection:
            for worker in runtime.workers.values():
                connection.execute(
                    """
                    INSERT INTO agent_runtime_state(
                      agent_id, worker_state, reliable_seq, dirty_since_seq,
                      pending_message_ids_json, pending_root_message_ids_json, active_run_id,
                      typing_status, restart_count, profile_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_id) DO UPDATE SET
                      worker_state = excluded.worker_state,
                      profile_hash = excluded.profile_hash,
                      updated_at = excluded.updated_at
                    """,
                    (
                        worker.agent_id,
                        worker.worker_state,
                        worker.reliable_seq,
                        worker.dirty_since_seq,
                        _json_list(worker.pending_message_ids),
                        _json_list(worker.pending_root_message_ids),
                        worker.active_run_id,
                        worker.typing_status,
                        worker.restart_count,
                        runtime.profile_hash,
                        utc_now(self.clock).isoformat(),
                    ),
                )
            connection.commit()

    def unregister_conversation(self, conversation_id: str) -> None:
        self._runtimes.pop(conversation_id, None)

    def pause_conversation(self, conversation_id: str) -> None:
        runtime = self._runtimes.get(conversation_id)
        if runtime is not None:
            runtime.paused = True
            for worker in runtime.workers.values():
                worker.worker_state = "PAUSED"

    def resume_conversation(self, conversation_id: str) -> None:
        runtime = self._runtimes.get(conversation_id)
        if runtime is not None:
            runtime.paused = False
            for worker in runtime.workers.values():
                if worker.worker_state == "PAUSED":
                    worker.worker_state = "LISTENING"

    def runtime_snapshot(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
    ) -> dict[str, Any]:
        if not _runtime_enabled(profile):
            return {
                "conversation_id": conversation.id,
                "profile_hash": conversation.profile_hash,
                "conversation_status": conversation.status,
                "workers": [],
            }
        self.register_conversation(conversation, profile=profile)
        runtime = self._runtimes[conversation.id]
        return {
            "conversation_id": conversation.id,
            "profile_hash": runtime.profile_hash,
            "conversation_status": conversation.status,
            "workers": [
                {
                    "agent_id": worker.agent_id,
                    "worker_state": worker.worker_state,
                    "reliable_seq": worker.reliable_seq,
                    "dirty_since_seq": worker.dirty_since_seq,
                    "active_run_id": worker.active_run_id,
                    "typing_status": worker.typing_status,
                    "restart_count": worker.restart_count,
                    "pending_message_ids": list(worker.pending_message_ids),
                }
                for worker in runtime.workers.values()
            ],
        }

    def ingest_message(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
        message: dict[str, Any],
        auto_pump: bool | None = None,
    ) -> None:
        if not _runtime_enabled(profile):
            return
        self.register_conversation(conversation, profile=profile)
        runtime = self._runtimes[conversation.id]
        if runtime.paused or conversation.status != ConversationStatus.RUNNING.value:
            return
        all_mention = _is_all_mention(profile, list(message["mentions"]))
        for worker in runtime.workers.values():
            message_seq = int(message["conversation_seq"])
            if message_seq <= worker.reliable_seq:
                continue
            worker.reliable_seq = message_seq
            if message["sender_id"] == worker.agent_id:
                worker.dirty_since_seq = None
                self._persist_worker_state(conversation, worker)
                continue
            should_queue = False
            if message["sender_kind"] == "user":
                mentions = set(cast(list[str], message["mentions"]))
                if not mentions or worker.agent_id in mentions or all_mention:
                    should_queue = True
                    worker.pending_root_message_ids.append(message["message_id"])
            elif worker.agent_id in message["mentions"]:
                should_queue = True
            if should_queue and message["message_id"] not in worker.pending_message_ids:
                worker.pending_message_ids.append(message["message_id"])
            if worker.active_run_id is not None and message["sender_id"] != worker.agent_id:
                worker.dirty_since_seq = message_seq
            self._persist_worker_state(conversation, worker)
        should_pump = self.auto_pump if auto_pump is None else auto_pump
        if should_pump:
            self.pump(conversation, profile=profile)

    def advance(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
        delta_ms: int,
    ) -> None:
        if not _runtime_enabled(profile):
            return
        if isinstance(self.clock, VirtualClock):
            self.clock.advance(timedelta(milliseconds=delta_ms))
        self.pump(conversation, profile=profile)

    def crash_worker(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
        agent_id: str,
        recoverable: bool,
    ) -> None:
        if not _runtime_enabled(profile):
            return
        self.register_conversation(conversation, profile=profile)
        runtime = self._runtimes[conversation.id]
        worker = runtime.workers[agent_id]
        self._stop_typing(conversation, worker, reason="crash")
        if recoverable and worker.restart_count < int(
            profile.metadata.get("worker_restart_limit", 1)
        ):
            worker.restart_count += 1
            worker.worker_state = "LISTENING"
            for message_id in worker.current_observation_ids:
                if message_id not in worker.pending_message_ids:
                    worker.pending_message_ids.append(message_id)
            for message_id in worker.current_root_message_ids:
                if message_id not in worker.pending_root_message_ids:
                    worker.pending_root_message_ids.append(message_id)
            worker.active_run_id = None
            worker.pending_call = None
            worker.current_decision = None
            worker.current_draft = None
            worker.current_observation_ids.clear()
            worker.current_root_message_ids.clear()
            worker.current_expected_seq = None
            worker.current_causal_episode_id = None
            worker.current_caused_by_message_id = None
            worker.current_agent_hop = 0
            worker.current_must_reply = False
            worker.current_all_mention = False
            worker.current_direct_mention = False
            self._persist_worker_state(conversation, worker)
            self.pump(conversation, profile=profile)
            return
        worker.worker_state = "ERROR"
        worker.active_run_id = None
        worker.pending_call = None
        worker.current_decision = None
        worker.current_draft = None
        worker.current_observation_ids.clear()
        worker.current_root_message_ids.clear()
        worker.current_expected_seq = None
        worker.current_causal_episode_id = None
        worker.current_caused_by_message_id = None
        worker.current_agent_hop = 0
        worker.current_must_reply = False
        worker.current_all_mention = False
        worker.current_direct_mention = False
        self._persist_worker_state(conversation, worker)

    def pump(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
        max_iterations: int = 100,
    ) -> None:
        if not _runtime_enabled(profile):
            return
        self.register_conversation(conversation, profile=profile)
        runtime = self._runtimes[conversation.id]
        if runtime.paused or conversation.status != ConversationStatus.RUNNING.value:
            return
        for _ in range(max_iterations):
            progressed = False
            for worker in runtime.workers.values():
                progressed = self._advance_worker(conversation, profile, worker) or progressed
            if not progressed:
                break

    def _advance_worker(
        self,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        worker: WorkerState,
    ) -> bool:
        if worker.worker_state in {"PAUSED", "ERROR", "STOPPED"}:
            return False
        if worker.pending_call is not None:
            if utc_now(self.clock) < worker.pending_call.ready_at:
                return False
            if worker.pending_call.purpose == "decision":
                self._complete_decision(conversation, profile, worker)
            else:
                self._complete_action(conversation, profile, worker)
            return True
        if worker.active_run_id is not None and worker.worker_state == "WAITING_TO_SEND":
            if (
                worker.earliest_send_at is not None
                and utc_now(self.clock) < worker.earliest_send_at
            ):
                return False
            self._submit_if_fresh(conversation, profile, worker)
            return True
        if worker.active_run_id is None and worker.pending_message_ids:
            self._start_run(conversation, profile, worker)
            return True
        return False

    def _start_run(
        self,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        worker: WorkerState,
    ) -> None:
        messages = self._messages_by_ids(conversation, worker.pending_message_ids)
        if not messages:
            worker.pending_message_ids.clear()
            worker.pending_root_message_ids.clear()
            self._persist_worker_state(conversation, worker)
            return
        mentions = set(messages[-1]["mentions"])
        all_mention = _is_all_mention(profile, list(mentions))
        direct_mention = worker.agent_id in mentions
        if (
            mentions
            and not direct_mention
            and not all_mention
            and messages[-1]["sender_kind"] == "user"
        ):
            worker.pending_message_ids.clear()
            worker.pending_root_message_ids.clear()
            self._persist_worker_state(conversation, worker)
            return
        run_id = generate_ulid(utc_now(self.clock))
        root_ids = list(worker.pending_root_message_ids)
        caused_by = messages[-1]["message_id"]
        if root_ids:
            causal_episode_id: str | None = _episode_id(root_ids)
            agent_hop = 0
        else:
            causal_episode_id = cast(str | None, messages[-1].get("causal_episode_id"))
            agent_hop = int(messages[-1].get("agent_hop") or 0) + 1
        worker.active_run_id = run_id
        worker.worker_state = "DECIDING"
        worker.current_observation_ids = list(worker.pending_message_ids)
        worker.current_root_message_ids = root_ids
        worker.current_expected_seq = int(messages[-1]["conversation_seq"])
        worker.current_causal_episode_id = causal_episode_id
        worker.current_caused_by_message_id = caused_by
        worker.current_agent_hop = agent_hop
        worker.current_must_reply = direct_mention or all_mention
        worker.current_all_mention = all_mention
        worker.current_direct_mention = direct_mention
        worker.pending_message_ids.clear()
        worker.pending_root_message_ids.clear()
        selected_step = self._select_step(
            conversation,
            profile,
            worker,
            purpose="decision",
            content="\n".join(message["content_markdown"] for message in messages),
        )
        worker.pending_call = PendingCall(
            purpose="decision",
            selected_step=selected_step,
            ready_at=utc_now(self.clock) + timedelta(milliseconds=selected_step.delay_ms),
        )
        self._write_run_row(
            conversation,
            worker,
            phase="DECIDING",
            status="RUNNING",
        )
        if worker.current_direct_mention:
            self._start_typing(conversation, worker)
        self._persist_worker_state(conversation, worker)

    def _complete_decision(
        self,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        worker: WorkerState,
    ) -> None:
        assert worker.pending_call is not None
        step = worker.pending_call.selected_step
        worker.pending_call = None
        if step.fail_code is not None:
            self._handle_worker_error(conversation, profile, worker, step.fail_code)
            return
        payload = step.output_json or self._safe_json(step.output_text)
        if not isinstance(payload, dict):
            payload = {"should_reply": False, "reason_codes": ["default_silence"]}
        should_reply = bool(payload.get("should_reply", worker.current_must_reply))
        if worker.current_must_reply:
            should_reply = True
        worker.current_decision = payload
        if not should_reply:
            self._finish_run(conversation, worker, status="SILENT", phase="DECIDING")
            return
        if self._should_invalidate(worker):
            self._invalidate_run(conversation, worker, reason="new_message_before_act")
            return
        selected_step = self._select_step(
            conversation,
            profile,
            worker,
            purpose="action",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        worker.worker_state = "ACTING"
        worker.pending_call = PendingCall(
            purpose="action",
            selected_step=selected_step,
            ready_at=utc_now(self.clock) + timedelta(milliseconds=selected_step.delay_ms),
        )
        self._write_run_row(
            conversation,
            worker,
            phase="ACTING",
            status="RUNNING",
            decision=payload,
        )
        if not worker.current_direct_mention:
            self._start_typing(conversation, worker)
        self._persist_worker_state(conversation, worker)

    def _complete_action(
        self,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        worker: WorkerState,
    ) -> None:
        assert worker.pending_call is not None
        step = worker.pending_call.selected_step
        worker.pending_call = None
        if step.fail_code is not None:
            self._handle_worker_error(conversation, profile, worker, step.fail_code)
            return
        payload = step.output_json or self._safe_json(step.output_text)
        if not isinstance(payload, dict) or "content_markdown" not in payload:
            payload = {
                "content_markdown": step.output_text or "ACK",
                "mentions": [],
                "primary_reply_to": worker.current_caused_by_message_id,
                "responds_to": [worker.current_caused_by_message_id]
                if worker.current_caused_by_message_id is not None
                else [],
            }
        worker.current_draft = payload
        if self._should_invalidate(worker):
            self._invalidate_run(conversation, worker, reason="new_message_before_wait")
            return
        wait_ms = _timing_wait_ms(
            profile,
            direct_mention=worker.current_direct_mention,
            content=str(payload["content_markdown"]),
        )
        worker.earliest_send_at = utc_now(self.clock) + timedelta(milliseconds=wait_ms)
        worker.worker_state = "WAITING_TO_SEND"
        self._write_run_row(
            conversation,
            worker,
            phase="WAITING_TO_SEND",
            status="RUNNING",
            decision=worker.current_decision,
            draft=payload,
            earliest_send_at=worker.earliest_send_at.isoformat(),
        )
        self._persist_worker_state(conversation, worker)

    def _submit_if_fresh(
        self,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        worker: WorkerState,
    ) -> None:
        if self._should_invalidate(worker):
            self._invalidate_run(conversation, worker, reason="new_message_before_submit")
            return
        assert worker.current_draft is not None
        assert worker.current_expected_seq is not None
        assert worker.active_run_id is not None
        try:
            message, _, _ = commit_message(
                conversation,
                profile=profile,
                payload=MessagePayload(
                    client_message_id=worker.active_run_id,
                    content_markdown=str(worker.current_draft["content_markdown"]),
                    mentions=list(worker.current_draft.get("mentions", [])),
                    primary_reply_to=worker.current_draft.get("primary_reply_to"),
                    responds_to=list(worker.current_draft.get("responds_to", [])),
                    sender_kind="agent",
                    sender_id=worker.agent_id,
                    expected_conversation_seq=worker.current_expected_seq,
                    idempotency_key=worker.active_run_id,
                    causal_episode_id=worker.current_causal_episode_id,
                    caused_by_message_id=worker.current_caused_by_message_id,
                    agent_hop=worker.current_agent_hop,
                ),
                clock=self.clock,
            )
        except AppError as exc:
            if exc.code == "stale_sequence":
                self._ingest_missed_messages(
                    conversation,
                    profile,
                    worker,
                    after_seq=worker.current_expected_seq,
                )
                if worker.current_all_mention:
                    worker.current_expected_seq = worker.reliable_seq
                    self._persist_worker_state(conversation, worker)
                    return
                self._invalidate_run(conversation, worker, reason="stale_sequence")
                return
            if exc.code == "budget_exhausted":
                self._finish_run(
                    conversation,
                    worker,
                    status="BUDGET_EXHAUSTED",
                    phase="SUBMITTING",
                    error_code=exc.code,
                    error_message=exc.message,
                )
                return
            self._finish_run(
                conversation,
                worker,
                status="ERROR",
                phase="SUBMITTING",
                error_code=exc.code,
                error_message=exc.message,
            )
            return
        self._stop_typing(conversation, worker, reason="submitted")
        self._finish_run(conversation, worker, status="COMMITTED", phase="SUBMITTING")
        self.ingest_message(conversation, profile=profile, message=message, auto_pump=False)

    def _ingest_missed_messages(
        self,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        worker: WorkerState,
        *,
        after_seq: int,
    ) -> None:
        for message in self._messages_after_seq(conversation, after_seq=after_seq):
            message_seq = int(message["conversation_seq"])
            if message_seq <= worker.reliable_seq:
                continue
            worker.reliable_seq = message_seq
            if (
                message["sender_id"] != worker.agent_id
                and message["message_id"] not in worker.pending_message_ids
            ):
                worker.pending_message_ids.append(message["message_id"])
                if message["sender_kind"] == "user":
                    worker.pending_root_message_ids.append(message["message_id"])
                worker.dirty_since_seq = message_seq
        self._persist_worker_state(conversation, worker)

    def _handle_worker_error(
        self,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        worker: WorkerState,
        code: str,
    ) -> None:
        recoverable = code != "WORKER_CRASHED_FATAL"
        if code.startswith("WORKER_CRASHED"):
            with connect_transcript(conversation) as connection:
                connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?, error_code = ?, error_message = ?,
                        updated_at = ?, finished_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        code,
                        code,
                        code,
                        utc_now(self.clock).isoformat(),
                        utc_now(self.clock).isoformat(),
                        worker.active_run_id,
                    ),
                )
                connection.commit()
            self.crash_worker(
                conversation,
                profile=profile,
                agent_id=worker.agent_id,
                recoverable=recoverable,
            )
            return
        self._finish_run(
            conversation,
            worker,
            status="ERROR",
            phase=worker.worker_state,
            error_code=code,
            error_message=code,
        )

    def _finish_run(
        self,
        conversation: ConversationRecord,
        worker: WorkerState,
        *,
        status: str,
        phase: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self._stop_typing(conversation, worker, reason=status.lower())
        with connect_transcript(conversation) as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, phase = ?, error_code = ?, error_message = ?,
                    updated_at = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    status,
                    phase,
                    error_code,
                    error_message,
                    utc_now(self.clock).isoformat(),
                    utc_now(self.clock).isoformat(),
                    worker.active_run_id,
                ),
            )
            connection.commit()
        worker.active_run_id = None
        worker.worker_state = "LISTENING"
        worker.current_observation_ids.clear()
        worker.current_root_message_ids.clear()
        worker.current_expected_seq = None
        worker.current_causal_episode_id = None
        worker.current_caused_by_message_id = None
        worker.current_agent_hop = 0
        worker.current_must_reply = False
        worker.current_all_mention = False
        worker.current_direct_mention = False
        worker.current_decision = None
        worker.current_draft = None
        worker.earliest_send_at = None
        worker.pending_call = None
        worker.dirty_since_seq = None
        self._persist_worker_state(conversation, worker)

    def _invalidate_run(
        self,
        conversation: ConversationRecord,
        worker: WorkerState,
        *,
        reason: str,
    ) -> None:
        with connect_transcript(conversation) as connection:
            connection.execute(
                """
                UPDATE agent_runs
                SET status = ?, error_code = ?, error_message = ?, invalidated_by_seq = ?,
                    updated_at = ?, finished_at = ?
                WHERE run_id = ?
                """,
                (
                    "INVALIDATED",
                    "STALE_DRAFT",
                    reason,
                    worker.dirty_since_seq,
                    utc_now(self.clock).isoformat(),
                    utc_now(self.clock).isoformat(),
                    worker.active_run_id,
                ),
            )
            connection.commit()
        self._finish_run(
            conversation,
            worker,
            status="INVALIDATED",
            phase="RECONSIDER_BEFORE_SEND",
            error_code="STALE_DRAFT",
            error_message=reason,
        )

    def _should_invalidate(self, worker: WorkerState) -> bool:
        if worker.dirty_since_seq is None or worker.current_expected_seq is None:
            return False
        if worker.dirty_since_seq <= worker.current_expected_seq:
            return False
        if worker.current_all_mention:
            return False
        return True

    def _safe_json(self, value: str) -> Any:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None

    def _select_step(
        self,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        worker: WorkerState,
        *,
        purpose: str,
        content: str,
    ) -> ScriptedStep:
        runtime = self._runtimes[conversation.id]
        adapter = runtime.adapters[worker.agent_id]
        from pal_chat_server.contracts import ModelRequest

        return adapter.select_step(
            ModelRequest(
                purpose=purpose,
                system_prompt=worker.agent_id,
                messages=[{"role": "user", "content": content}],
                metadata={
                    "agent_id": worker.agent_id,
                    "profile_hash": profile.metadata.get("profile_hash", conversation.profile_hash),
                },
            )
        )

    def _messages_by_ids(
        self,
        conversation: ConversationRecord,
        message_ids: list[str],
    ) -> list[dict[str, Any]]:
        if not message_ids:
            return []
        placeholders = ",".join("?" for _ in message_ids)
        with connect_transcript(conversation) as connection:
            rows = connection.execute(
                f"""
                SELECT message_id, conversation_seq, sender_kind, sender_id, content_markdown,
                       primary_reply_to, causal_episode_id, caused_by_message_id, agent_hop
                FROM messages
                WHERE message_id IN ({placeholders})
                ORDER BY conversation_seq
                """,
                tuple(message_ids),
            ).fetchall()
            mentions_by_id = {
                row["message_id"]: [
                    mention["member_id"]
                    for mention in connection.execute(
                        """
                        SELECT member_id
                        FROM message_mentions
                        WHERE message_id = ?
                        ORDER BY member_id
                        """,
                        (row["message_id"],),
                    ).fetchall()
                ]
                for row in rows
            }
        by_id = {
            row["message_id"]: {
                "message_id": row["message_id"],
                "conversation_seq": int(row["conversation_seq"]),
                "sender_kind": row["sender_kind"],
                "sender_id": row["sender_id"],
                "content_markdown": row["content_markdown"],
                "primary_reply_to": row["primary_reply_to"],
                "causal_episode_id": row["causal_episode_id"],
                "caused_by_message_id": row["caused_by_message_id"],
                "agent_hop": int(row["agent_hop"]),
                "mentions": mentions_by_id[row["message_id"]],
            }
            for row in rows
        }
        return [by_id[item] for item in message_ids if item in by_id]

    def _messages_after_seq(
        self,
        conversation: ConversationRecord,
        *,
        after_seq: int,
    ) -> list[dict[str, Any]]:
        rows = list_messages(conversation, after_seq=after_seq, limit=200)
        message_ids = [row["message_id"] for row in rows]
        return self._messages_by_ids(conversation, message_ids)

    def _persist_worker_state(self, conversation: ConversationRecord, worker: WorkerState) -> None:
        with connect_transcript(conversation) as connection:
            connection.execute(
                """
                INSERT INTO agent_runtime_state(
                  agent_id, worker_state, reliable_seq, dirty_since_seq,
                  pending_message_ids_json, pending_root_message_ids_json, active_run_id,
                  typing_status, restart_count, profile_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                  worker_state = excluded.worker_state,
                  reliable_seq = excluded.reliable_seq,
                  dirty_since_seq = excluded.dirty_since_seq,
                  pending_message_ids_json = excluded.pending_message_ids_json,
                  pending_root_message_ids_json = excluded.pending_root_message_ids_json,
                  active_run_id = excluded.active_run_id,
                  typing_status = excluded.typing_status,
                  restart_count = excluded.restart_count,
                  profile_hash = excluded.profile_hash,
                  updated_at = excluded.updated_at
                """,
                (
                    worker.agent_id,
                    worker.worker_state,
                    worker.reliable_seq,
                    worker.dirty_since_seq,
                    _json_list(worker.pending_message_ids),
                    _json_list(worker.pending_root_message_ids),
                    worker.active_run_id,
                    worker.typing_status,
                    worker.restart_count,
                    worker.profile_hash,
                    utc_now(self.clock).isoformat(),
                ),
            )
            connection.commit()

    def _write_run_row(
        self,
        conversation: ConversationRecord,
        worker: WorkerState,
        *,
        phase: str,
        status: str,
        decision: dict[str, Any] | None = None,
        draft: dict[str, Any] | None = None,
        earliest_send_at: str | None = None,
    ) -> None:
        assert worker.active_run_id is not None
        with connect_transcript(conversation) as connection:
            connection.execute(
                """
                INSERT INTO agent_runs(
                  run_id, agent_id, status, phase, observation_message_ids_json,
                  root_message_ids_json, expected_conversation_seq, profile_hash,
                  idempotency_key, causal_episode_id, caused_by_message_id, agent_hop,
                  decision_json, draft_message_json, earliest_send_at, invalidated_by_seq,
                  error_code, error_message, started_at, updated_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status = excluded.status,
                  phase = excluded.phase,
                  decision_json = excluded.decision_json,
                  draft_message_json = excluded.draft_message_json,
                  earliest_send_at = excluded.earliest_send_at,
                  updated_at = excluded.updated_at
                """,
                (
                    worker.active_run_id,
                    worker.agent_id,
                    status,
                    phase,
                    _json_list(worker.current_observation_ids),
                    _json_list(worker.current_root_message_ids),
                    worker.current_expected_seq or 0,
                    worker.profile_hash,
                    worker.active_run_id,
                    worker.current_causal_episode_id,
                    worker.current_caused_by_message_id,
                    worker.current_agent_hop,
                    json.dumps(decision, ensure_ascii=False, sort_keys=True) if decision else None,
                    json.dumps(draft, ensure_ascii=False, sort_keys=True) if draft else None,
                    earliest_send_at,
                    None,
                    None,
                    None,
                    utc_now(self.clock).isoformat(),
                    utc_now(self.clock).isoformat(),
                    None,
                ),
            )
            connection.commit()

    def _emit_runtime_event(
        self,
        conversation: ConversationRecord,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        now = utc_now(self.clock).isoformat()
        with connect_transcript(conversation) as connection:
            connection.execute(
                """
                INSERT INTO outbox_events(
                  event_id, event_type, conversation_seq, payload_json, status,
                  dispatch_attempts, dispatched_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generate_ulid(),
                    event_type,
                    None,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "pending",
                    0,
                    None,
                    now,
                ),
            )
            connection.commit()
        dispatch_pending_outbox(conversation, clock=self.clock)

    def _start_typing(self, conversation: ConversationRecord, worker: WorkerState) -> None:
        if worker.typing_status == "active":
            return
        worker.typing_status = "active"
        self._persist_worker_state(conversation, worker)
        self._emit_runtime_event(
            conversation,
            event_type="agent.typing_started",
            payload={"agent_id": worker.agent_id, "run_id": worker.active_run_id},
        )

    def _stop_typing(
        self,
        conversation: ConversationRecord,
        worker: WorkerState,
        *,
        reason: str,
    ) -> None:
        if worker.typing_status == "idle":
            return
        worker.typing_status = "idle"
        self._persist_worker_state(conversation, worker)
        self._emit_runtime_event(
            conversation,
            event_type="agent.typing_stopped",
            payload={"agent_id": worker.agent_id, "run_id": worker.active_run_id, "reason": reason},
        )
