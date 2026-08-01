from __future__ import annotations

import asyncio
import concurrent.futures
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from fastapi import WebSocket

from pal_chat_server.agent_runtime import is_agent_runtime_enabled
from pal_chat_server.attempts import mark_run_attempts_crashed
from pal_chat_server.errors import AppError
from pal_chat_server.models import ConversationRecord
from pal_chat_server.schemas import ExperimentProfile
from pal_chat_server.sequence_runtime import connect_transcript


def _python_path() -> str:
    return str(Path(__file__).resolve().parents[1])


@dataclass(slots=True)
class WorkerAuthContext:
    conversation_id: str
    agent_id: str
    profile_hash: str
    token: str
    pid: int
    connected: bool = False
    port: int | None = None
    restart_count: int = 0


@dataclass(slots=True)
class WorkerProcessHandle:
    conversation_id: str
    agent_id: str
    profile_hash: str
    process: subprocess.Popen[bytes]
    token: str
    restart_count: int = 0
    connected: threading.Event = field(default_factory=threading.Event)
    pause_ack: threading.Event = field(default_factory=threading.Event)
    stopped: bool = False


class InternalWorkerSocketManager:
    def __init__(self) -> None:
        self._connections: dict[str, dict[str, tuple[WebSocket, asyncio.AbstractEventLoop]]] = {}
        self._lock = threading.Lock()

    async def connect(self, conversation_id: str, agent_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        loop = asyncio.get_running_loop()
        with self._lock:
            self._connections.setdefault(conversation_id, {})[agent_id] = (websocket, loop)

    def disconnect(self, conversation_id: str, agent_id: str) -> None:
        with self._lock:
            if conversation_id not in self._connections:
                return
            self._connections[conversation_id].pop(agent_id, None)
            if not self._connections[conversation_id]:
                self._connections.pop(conversation_id, None)

    def publish(
        self,
        conversation_id: str,
        event: dict[str, Any],
        *,
        target_agent_id: str | None = None,
    ) -> None:
        def handle_delivery_result(
            future: concurrent.futures.Future[None],
            *,
            current_conversation_id: str,
            current_agent_id: str,
        ) -> None:
            try:
                future.result()
            except Exception:
                self.disconnect(current_conversation_id, current_agent_id)

        with self._lock:
            current = dict(self._connections.get(conversation_id, {}))
        for agent_id, (websocket, loop) in current.items():
            if target_agent_id is not None and agent_id != target_agent_id:
                continue
            future = asyncio.run_coroutine_threadsafe(websocket.send_json(event), loop)
            
            def callback(
                done: concurrent.futures.Future[None],
                *,
                current_conversation_id: str = conversation_id,
                current_agent_id: str = agent_id,
            ) -> None:
                handle_delivery_result(
                    done,
                    current_conversation_id=current_conversation_id,
                    current_agent_id=current_agent_id,
                )

            future.add_done_callback(callback)


INTERNAL_SOCKET_MANAGER = InternalWorkerSocketManager()


class WorkerSupervisor:
    def __init__(self, *, server_base_url: str | None) -> None:
        self.server_base_url = server_base_url.rstrip("/") if server_base_url else None
        self._handles: dict[str, dict[str, WorkerProcessHandle]] = {}
        self._auth: dict[str, WorkerAuthContext] = {}
        self._lock = threading.Lock()
        self._monitors: dict[str, threading.Thread] = {}
        self._stopping: set[str] = set()

    @property
    def enabled(self) -> bool:
        return self.server_base_url is not None

    def has_process_mode(self, profile: ExperimentProfile) -> bool:
        return self.enabled and is_agent_runtime_enabled(profile)

    def runtime_snapshot(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
    ) -> dict[str, Any]:
        with self._lock:
            handles = dict(self._handles.get(conversation.id, {}))
        return {
            "conversation_id": conversation.id,
            "profile_hash": conversation.profile_hash,
            "conversation_status": conversation.status,
            "workers": [
                {
                    "agent_id": agent_id,
                    "pid": handle.process.pid,
                    "connected": handle.connected.is_set(),
                    "alive": handle.process.poll() is None,
                    "pause_ack": handle.pause_ack.is_set(),
                    "restart_count": handle.restart_count,
                }
                for agent_id, handle in handles.items()
            ],
        }

    def start_workers(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
        guardrails: dict[str, Any],
    ) -> None:
        if not self.has_process_mode(profile):
            return
        assert conversation.profile_hash is not None
        with self._lock:
            if conversation.id in self._handles:
                return
        handles = {
            profile.agent_a.agent_id: self._spawn_worker(
                conversation=conversation,
                profile=profile,
                agent_id=profile.agent_a.agent_id,
                guardrails=guardrails,
                restart_count=0,
            ),
            profile.agent_b.agent_id: self._spawn_worker(
                conversation=conversation,
                profile=profile,
                agent_id=profile.agent_b.agent_id,
                guardrails=guardrails,
                restart_count=0,
            ),
        }
        with self._lock:
            self._handles[conversation.id] = handles
        self._start_monitor(conversation, profile=profile, guardrails=guardrails)
        deadline = time.time() + 10
        while time.time() < deadline:
            if all(handle.connected.wait(timeout=0.1) for handle in handles.values()):
                return
        self.stop_workers(conversation.id)
        raise AppError(
            code="worker_start_timeout",
            status_code=409,
            message="Workers failed to become ready.",
        )

    def pause_workers(self, conversation_id: str) -> None:
        with self._lock:
            handles = list(self._handles.get(conversation_id, {}).values())
        for handle in handles:
            handle.pause_ack.clear()
        self.publish_control(
            conversation_id,
            {"event_type": "conversation.state_changed", "state": "paused"},
        )
        deadline = time.time() + 5
        while time.time() < deadline:
            if all(handle.pause_ack.wait(timeout=0.1) for handle in handles):
                return
        raise AppError(
            code="pause_checkpoint_timeout",
            status_code=409,
            message="Workers failed to acknowledge pause checkpoint.",
        )

    def resume_workers(self, conversation_id: str) -> None:
        self.publish_control(
            conversation_id,
            {"event_type": "conversation.state_changed", "state": "running"},
        )

    def stop_workers(self, conversation_id: str) -> None:
        with self._lock:
            current = self._handles.get(conversation_id, {})
            for handle in current.values():
                handle.stopped = True
            self._stopping.add(conversation_id)
            handles = self._handles.pop(conversation_id, {})
        for handle in handles.values():
            INTERNAL_SOCKET_MANAGER.disconnect(conversation_id, handle.agent_id)
            self._auth.pop(handle.token, None)
            if handle.process.poll() is None:
                handle.process.terminate()
                try:
                    handle.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    handle.process.kill()
                    handle.process.wait(timeout=5)
        with self._lock:
            self._stopping.discard(conversation_id)
            self._monitors.pop(conversation_id, None)

    def shutdown(self) -> None:
        with self._lock:
            conversation_ids = list(self._handles)
        for conversation_id in conversation_ids:
            self.stop_workers(conversation_id)

    def publish_public_event(self, conversation_id: str, event: dict[str, Any]) -> None:
        INTERNAL_SOCKET_MANAGER.publish(conversation_id, event)

    def publish_control(self, conversation_id: str, payload: dict[str, Any]) -> None:
        event = {
            "protocol_version": 1,
            "event_id": token_urlsafe(8),
            "event_type": payload["event_type"],
            "conversation_id": conversation_id,
            "conversation_seq": None,
            "payload": payload,
        }
        INTERNAL_SOCKET_MANAGER.publish(conversation_id, event)

    def verify(
        self,
        *,
        conversation_id: str,
        token: str,
        agent_id: str,
        profile_hash: str,
    ) -> WorkerAuthContext:
        context = self._auth.get(token)
        if context is None:
            raise AppError(
                code="internal_auth_failed",
                status_code=401,
                message="Internal token is invalid.",
            )
        if (
            context.conversation_id != conversation_id
            or context.agent_id != agent_id
            or context.profile_hash != profile_hash
        ):
            raise AppError(
                code="internal_auth_failed",
                status_code=403,
                message="Internal identity tuple mismatch.",
            )
        return context

    def mark_connected(
        self,
        *,
        conversation_id: str,
        token: str,
        agent_id: str,
        profile_hash: str,
    ) -> WorkerAuthContext:
        context = self.verify(
            conversation_id=conversation_id,
            token=token,
            agent_id=agent_id,
            profile_hash=profile_hash,
        )
        context.connected = True
        with self._lock:
            handle = self._handles.get(conversation_id, {}).get(agent_id)
        if handle is not None:
            handle.connected.set()
        return context

    def record_checkpoint_ack(self, *, conversation_id: str, agent_id: str) -> None:
        with self._lock:
            handle = self._handles.get(conversation_id, {}).get(agent_id)
        if handle is not None:
            handle.pause_ack.set()

    def _spawn_worker(
        self,
        *,
        conversation: ConversationRecord,
        profile: ExperimentProfile,
        agent_id: str,
        guardrails: dict[str, Any],
        restart_count: int,
    ) -> WorkerProcessHandle:
        assert self.server_base_url is not None
        assert conversation.profile_hash is not None
        token = token_urlsafe(24)
        archive_dir = Path(conversation.archive_dir or ".")
        log_dir = archive_dir / "module-state" / agent_id
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "worker-process.log"
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{_python_path()}:{env.get('PYTHONPATH', '')}".rstrip(":")
        env["PAL_CHAT_SERVER_BASE_URL"] = self.server_base_url
        env["PAL_CHAT_CONVERSATION_ID"] = conversation.id
        env["PAL_CHAT_AGENT_ID"] = agent_id
        env["PAL_CHAT_PROFILE_HASH"] = conversation.profile_hash
        env["PAL_CHAT_WORKER_TOKEN"] = token
        env["PAL_CHAT_WORKER_RESTART_LIMIT"] = str(guardrails.get("worker_restart_limit", 1))
        with log_path.open("ab") as handle:
            process = subprocess.Popen(
                [sys.executable, "-m", "pal_chat_server.worker_main"],
                env=env,
                stdout=handle,
                stderr=handle,
            )
        self._auth[token] = WorkerAuthContext(
            conversation_id=conversation.id,
            agent_id=agent_id,
            profile_hash=conversation.profile_hash,
            token=token,
            pid=process.pid,
            restart_count=restart_count,
        )
        return WorkerProcessHandle(
            conversation_id=conversation.id,
            agent_id=agent_id,
            profile_hash=conversation.profile_hash,
            process=process,
            token=token,
            restart_count=restart_count,
        )

    def _start_monitor(
        self,
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
        guardrails: dict[str, Any],
    ) -> None:
        if conversation.id in self._monitors:
            return

        def monitor() -> None:
            while True:
                with self._lock:
                    handles = dict(self._handles.get(conversation.id, {}))
                if not handles:
                    return
                for agent_id, handle in handles.items():
                    code = handle.process.poll()
                    if code is None or handle.stopped:
                        continue
                    handle.connected.clear()
                    self._auth.pop(handle.token, None)
                    with self._lock:
                        stopping = conversation.id in self._stopping
                    if stopping:
                        continue
                    self._record_process_exit(
                        conversation,
                        agent_id=agent_id,
                        restart_count=handle.restart_count,
                        code=code,
                    )
                    restart_limit = int(guardrails.get("worker_restart_limit", 1))
                    if code == 91 or handle.restart_count >= restart_limit:
                        continue
                    new_handle = self._spawn_worker(
                        conversation=conversation,
                        profile=profile,
                        agent_id=agent_id,
                        guardrails=guardrails,
                        restart_count=handle.restart_count + 1,
                    )
                    with self._lock:
                        if conversation.id in self._handles:
                            self._handles[conversation.id][agent_id] = new_handle
                time.sleep(0.1)

        thread = threading.Thread(target=monitor, daemon=True)
        self._monitors[conversation.id] = thread
        thread.start()

    def _record_process_exit(
        self,
        conversation: ConversationRecord,
        *,
        agent_id: str,
        restart_count: int,
        code: int,
    ) -> None:
        run_status = "INVALIDATED" if code != 91 else "FATAL"
        worker_state = "RESTARTING" if code != 91 else "ERROR"
        error_code = f"worker_exit_{code}"
        error_message = f"Worker process exited with code {code}."
        with connect_transcript(conversation) as connection:
            active_row = connection.execute(
                """
                SELECT active_run_id
                FROM agent_runtime_state
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            active_run_id = active_row["active_run_id"] if active_row is not None else None
            connection.execute(
                """
                INSERT INTO agent_runtime_state(
                  agent_id, worker_state, reliable_seq, dirty_since_seq,
                  pending_message_ids_json, pending_root_message_ids_json, active_run_id,
                  typing_status, typing_run_id, restart_count, profile_hash, updated_at
                )
                SELECT
                  agent_id, ?, reliable_seq, dirty_since_seq,
                  pending_message_ids_json, pending_root_message_ids_json, NULL,
                  'idle', NULL, ?, profile_hash, CURRENT_TIMESTAMP
                FROM agent_runtime_state
                WHERE agent_id = ?
                ON CONFLICT(agent_id) DO UPDATE SET
                  worker_state = excluded.worker_state,
                  active_run_id = NULL,
                  typing_status = excluded.typing_status,
                  typing_run_id = excluded.typing_run_id,
                  restart_count = excluded.restart_count,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    worker_state,
                    restart_count,
                    agent_id,
                ),
            )
            if active_run_id is not None:
                mark_run_attempts_crashed(
                    connection,
                    conversation_id=conversation.id,
                    run_id=str(active_run_id),
                    finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    error_code=error_code,
                    error_message=error_message,
                )
                connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = ?,
                        phase = ?,
                        error_code = ?,
                        error_message = ?,
                        finished_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = ?
                    """,
                    (
                        run_status,
                        worker_state,
                        error_code,
                        error_message,
                        active_run_id,
                    ),
                )
            connection.commit()
