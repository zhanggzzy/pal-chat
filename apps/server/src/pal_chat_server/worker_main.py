from __future__ import annotations

import base64
import json
import os
import queue
import secrets
import signal
import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from hashlib import sha1
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode, urlparse

import httpx

from pal_chat_server.contracts import ModelRequest
from pal_chat_server.private_runtime import PrivateRuntimeStore
from pal_chat_server.scripted_adapter import ScriptedModelAdapter, ScriptedStep


@dataclass(slots=True)
class WorkerConfig:
    server_base_url: str
    conversation_id: str
    agent_id: str
    profile_hash: str
    token: str


@dataclass(slots=True)
class WorkerState:
    reliable_seq: int = 0
    active_run_id: str | None = None
    expected_conversation_seq: int | None = None
    dirty_since_seq: int | None = None
    pending_messages: list[dict[str, Any]] = field(default_factory=list)
    pending_roots: list[str] = field(default_factory=list)
    current_all_mention: bool = False
    current_direct_mention: bool = False
    caused_by_message_id: str | None = None
    causal_episode_id: str | None = None
    agent_hop: int = 0
    paused: bool = False
    typing_active: bool = False
    pause_requested: bool = False
    episode_root_message_ids: dict[str, str] = field(default_factory=dict)


class PhaseExecutionError(Exception):
    def __init__(self, *, error_code: str, error_message: str) -> None:
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message


class AttemptBudgetRejectedError(Exception):
    def __init__(self, *, error_code: str, error_message: str) -> None:
        super().__init__(error_message)
        self.error_code = error_code
        self.error_message = error_message


def env_config() -> WorkerConfig:
    return WorkerConfig(
        server_base_url=os.environ["PAL_CHAT_SERVER_BASE_URL"].rstrip("/"),
        conversation_id=os.environ["PAL_CHAT_CONVERSATION_ID"],
        agent_id=os.environ["PAL_CHAT_AGENT_ID"],
        profile_hash=os.environ["PAL_CHAT_PROFILE_HASH"],
        token=os.environ["PAL_CHAT_WORKER_TOKEN"],
    )


def auth_headers(config: WorkerConfig) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.token}",
        "X-Agent-ID": config.agent_id,
        "X-Profile-Hash": config.profile_hash,
    }


class RawWebSocketClient:
    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers
        self.sock: socket.socket | None = None
        self._closed = False

    def connect(self) -> None:
        parsed = urlparse(self.url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path
        if parsed.query:
            path = f"{path}?{parsed.query}"
        sock = socket.create_connection((host, port), timeout=5)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        request.extend(f"{name}: {value}" for name, value in self.headers.items())
        sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode("utf-8"))
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake failed")
            response += chunk
        status_line, _ = response.split(b"\r\n", 1)
        if b"101" not in status_line:
            status_text = status_line.decode("utf-8", "ignore")
            raise RuntimeError(f"websocket handshake rejected: {status_text}")
        sock.settimeout(None)
        self.sock = sock

    def recv_json(self) -> dict[str, Any]:
        if self.sock is None:
            raise RuntimeError("websocket not connected")
        first = self.sock.recv(2)
        if len(first) < 2:
            raise RuntimeError("websocket closed")
        first_byte, second_byte = first
        opcode = first_byte & 0x0F
        payload_length = second_byte & 0x7F
        if payload_length == 126:
            payload_length = struct.unpack("!H", self.sock.recv(2))[0]
        elif payload_length == 127:
            payload_length = struct.unpack("!Q", self.sock.recv(8))[0]
        payload = b""
        while len(payload) < payload_length:
            payload += self.sock.recv(payload_length - len(payload))
        if opcode == 8:
            raise RuntimeError("websocket closed")
        if opcode != 1:
            return {}
        decoded = json.loads(payload.decode("utf-8"))
        return decoded if isinstance(decoded, dict) else {}

    def close(self) -> None:
        if self.sock is not None and not self._closed:
            try:
                self.sock.close()
            finally:
                self._closed = True


def select_script(payload: dict[str, Any]) -> list[ScriptedStep]:
    profile = payload["profile"]
    modules = profile["modules"]
    adapter_config = modules["model_adapter"]["config"]
    return [ScriptedStep.model_validate(item) for item in adapter_config.get("script", [])]


def timing_wait_ms(payload: dict[str, Any], *, direct_mention: bool, content: str) -> int:
    config = payload["profile"]["modules"]["timing"]["config"]
    base_ms = int(config.get("base_wait_ms", 0))
    per_char_ms = int(config.get("per_char_wait_ms", 0))
    minimum_typing_ms = int(config.get("minimum_typing_ms", 0))
    wait_ms = base_ms + len(content) * per_char_ms
    return max(minimum_typing_ms, wait_ms) if direct_mention else max(0, wait_ms)


def guardrails_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("guardrails", {})
    return dict(value) if isinstance(value, dict) else {}


def decision_request(agent_id: str, message: dict[str, Any]) -> ModelRequest:
    return ModelRequest(
        purpose="decision",
        system_prompt="",
        messages=[{"content": str(message["content_markdown"])}],
        metadata={"agent_id": agent_id},
    )


def action_request(agent_id: str, decision_payload: dict[str, Any]) -> ModelRequest:
    return ModelRequest(
        purpose="action",
        system_prompt="",
        messages=[
            {"content": json.dumps(decision_payload, ensure_ascii=False, sort_keys=True)}
        ],
        metadata={"agent_id": agent_id},
    )


def serialized_output(step: ScriptedStep) -> str:
    if step.output_json is not None:
        return json.dumps(step.output_json, ensure_ascii=False, sort_keys=True)
    return step.output_text


def classify_failure(
    fail_code: str,
    *,
    retry_index: int,
) -> tuple[str, str, bool, int]:
    if fail_code == "TIMEOUT":
        schedule = [100, 200]
        return (
            "timeout",
            "Model request timed out.",
            retry_index < len(schedule),
            schedule[retry_index] if retry_index < len(schedule) else 0,
        )
    if fail_code == "HTTP_429":
        schedule = [200, 400]
        return (
            "http_429",
            "Model provider returned HTTP 429.",
            retry_index < len(schedule),
            schedule[retry_index] if retry_index < len(schedule) else 0,
        )
    if fail_code.startswith("HTTP_5") or fail_code == "RETRYABLE_5XX":
        schedule = [150, 300]
        return (
            "http_5xx",
            "Model provider returned a retryable 5xx error.",
            retry_index < len(schedule),
            schedule[retry_index] if retry_index < len(schedule) else 0,
        )
    return ("non_retryable", f"Model request failed: {fail_code}", False, 0)


def fetch_runtime_snapshot(client: httpx.Client, config: WorkerConfig) -> dict[str, Any]:
    response = client.get(
        f"{config.server_base_url}/internal/v1/conversations/{config.conversation_id}/runtime-snapshot",
        headers=auth_headers(config),
    )
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def fetch_recent_messages(
    client: httpx.Client,
    config: WorkerConfig,
    *,
    after_seq: int,
) -> list[dict[str, Any]]:
    response = client.get(
        f"{config.server_base_url}/internal/v1/conversations/{config.conversation_id}/messages",
        headers=auth_headers(config),
        params={"after_seq": max(0, after_seq)},
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("items", [])
    return items if isinstance(items, list) else []


def normalize_memory_operations(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    memory_delta = payload.get("memory_delta")
    if not isinstance(memory_delta, dict):
        return []
    operations = memory_delta.get("operations", [])
    if not isinstance(operations, list):
        return []
    return [item for item in operations if isinstance(item, dict)]


def should_queue(
    payload: dict[str, Any],
    state: WorkerState,
    agent_id: str,
    message: dict[str, Any],
) -> bool:
    mentions = set(message.get("mentions", []))
    all_mention = mentions.issuperset(payload["all_agent_ids"])
    if message["sender_id"] == agent_id:
        return False
    if message["sender_kind"] == "user":
        return not mentions or agent_id in mentions or all_mention
    if agent_id not in mentions:
        return False
    if is_root_stage_peer_handoff(state, message):
        relay_owner = sorted(str(item) for item in payload["all_agent_ids"])[0]
        return agent_id == relay_owner
    return True


def is_root_stage_peer_handoff(
    state: WorkerState,
    message: dict[str, Any],
) -> bool:
    episode_id = str(message.get("causal_episode_id") or "")
    root_message_id = state.episode_root_message_ids.get(episode_id)
    return bool(
        episode_id
        and root_message_id
        and str(message.get("caused_by_message_id") or "") == root_message_id
        and int(message.get("agent_hop") or 0) == 0
    )


def run_worker() -> None:
    config = env_config()
    client = httpx.Client(timeout=10)
    stop_event = threading.Event()

    def stop_handler(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    runtime_payload = fetch_runtime_snapshot(client, config)
    script = select_script(runtime_payload)
    adapter = ScriptedModelAdapter(script)
    state = WorkerState(
        reliable_seq=int(
            runtime_payload.get("reliable_seq", runtime_payload["latest_conversation_seq"])
        )
    )
    profile = runtime_payload["profile"]
    modules = profile["modules"]
    agent_profile = (
        profile["agent_a"]
        if profile["agent_a"]["agent_id"] == config.agent_id
        else profile["agent_b"]
    )
    runtime_store = PrivateRuntimeStore(
        state_dir=Path(str(runtime_payload["state_dir"])),
        agent_id=config.agent_id,
        attention_prior=str(agent_profile["attention_prior"]),
        prompt_version=str(profile.get("prompt_version", "phase1")),
        memory_module_id=str(modules["memory"]["module_id"]),
        memory_config=dict(modules["memory"].get("config", {})),
        context_module_id=str(modules["context_assembly"]["module_id"]),
        context_config=dict(modules["context_assembly"].get("config", {})),
    )
    incoming: queue.Queue[dict[str, Any]] = queue.Queue()

    ws_url = config.server_base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = (
        f"{ws_url}/internal/v1/conversations/{config.conversation_id}/agent-stream?"
        + urlencode({"after_seq": state.reliable_seq})
    )
    ws = RawWebSocketClient(ws_url, auth_headers(config))
    ws.connect()

    def listen() -> None:
        while not stop_event.is_set():
            try:
                event = ws.recv_json()
            except Exception:
                stop_event.set()
                return
            if event:
                incoming.put(event)

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()

    def post_status(
        status: str,
        phase: str,
        run_id: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        run_segment = run_id or "-"
        payload = {
            "worker_state": phase,
            "reliable_seq": state.reliable_seq,
            "dirty_since_seq": state.dirty_since_seq,
            "active_run_id": run_id,
            "run_status": status,
            "typing_action": "start" if state.typing_active else "stop",
            "causal_episode_id": state.causal_episode_id,
            "caused_by_message_id": state.caused_by_message_id,
            "agent_hop": state.agent_hop,
            "checkpoint_ack": "paused" if state.pause_requested else None,
        }
        if extra:
            payload.update(extra)
        client.post(
            f"{config.server_base_url}/internal/v1/conversations/"
            f"{config.conversation_id}/agent-runs/{run_segment}/status",
            headers=auth_headers(config),
            json=payload,
        ).raise_for_status()

    def post_worker_state(phase: str) -> None:
        post_status("IDLE", phase, None)

    def set_typing(active: bool, run_id: str | None, reason: str) -> None:
        if state.typing_active == active:
            return
        state.typing_active = active
        run_segment = run_id or "-"
        client.post(
            f"{config.server_base_url}/internal/v1/conversations/"
            f"{config.conversation_id}/agent-runs/{run_segment}/typing",
            headers=auth_headers(config),
            json={"action": "start" if active else "stop", "reason": reason},
        ).raise_for_status()

    def register_attempt(
        *,
        run_id: str,
        phase: str,
        bundle_revision: str,
        memory_revision_before: str,
        staged_memory_revision: str | None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = client.post(
            f"{config.server_base_url}/internal/v1/conversations/"
            f"{config.conversation_id}/agent-runs/{run_id}/attempts/register",
            headers=auth_headers(config),
            json={
                "phase": phase,
                "bundle_revision": bundle_revision,
                "memory_revision_before": memory_revision_before,
                "staged_memory_revision": staged_memory_revision,
                "payload": payload or {},
            },
        )
        response.raise_for_status()
        registered = response.json()
        return registered if isinstance(registered, dict) else {}

    def finalize_attempt(
        run_id: str,
        attempt_id: str,
        *,
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
    ) -> None:
        response = client.post(
            f"{config.server_base_url}/internal/v1/conversations/"
            f"{config.conversation_id}/agent-runs/{run_id}/attempts/{attempt_id}/finalize",
            headers=auth_headers(config),
            json={
                "status": status,
                "payload": payload,
                "error_code": error_code,
                "error_message": error_message,
                "error_class": error_class,
                "retryable": retryable,
                "backoff_ms": backoff_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost_usd,
                "memory_revision_after": memory_revision_after,
            },
        )
        response.raise_for_status()

    def handle_selected_step(step: ScriptedStep) -> None:
        if step.fail_code == "WORKER_CRASHED":
            os._exit(90)
        if step.fail_code == "WORKER_CRASHED_FATAL":
            os._exit(91)

    def build_context_trace(
        *,
        bundle: dict[str, Any],
        memory_revision_before: str,
        staged_memory_revision: str | None,
        run_guardrails: dict[str, Any],
        memory_revision_after: str | None = None,
        should_reply: bool | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        trace = {
            "bundle_id": bundle["bundle_id"],
            "bundle_revision": bundle["revision"],
            "phase": bundle["phase"],
            "conversation_seq": bundle["conversation_seq"],
            "projection_revision": bundle["projection_revision"],
            "memory_revision_before": memory_revision_before,
            "staged_memory_revision": staged_memory_revision,
            "selected_public_refs": bundle["selected_public_refs"],
            "selected_private_refs": bundle["selected_private_refs"],
            "estimated_tokens": bundle["estimated_tokens"],
            "guardrails_snapshot": run_guardrails,
        }
        if memory_revision_after is not None:
            trace["memory_revision_after"] = memory_revision_after
        if should_reply is not None:
            trace["should_reply"] = should_reply
        if payload is not None:
            trace["payload"] = payload
        return trace

    def run_model_phase(
        *,
        phase: str,
        request: ModelRequest,
        bundle_revision: str,
        memory_revision_before: str,
        staged_memory_revision: str | None,
        fallback_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        retry_index = 0
        haystack = "\n".join(message["content"] for message in request.messages)
        while True:
            assert state.active_run_id is not None
            run_id = str(state.active_run_id)
            registered = register_attempt(
                run_id=run_id,
                phase=phase,
                bundle_revision=bundle_revision,
                memory_revision_before=memory_revision_before,
                staged_memory_revision=staged_memory_revision,
                payload={"purpose": request.purpose, "retry_index": retry_index},
            )
            attempt_id = str(registered["attempt_id"])
            if not bool(registered.get("admitted", False)):
                error = cast(dict[str, Any], registered.get("error") or {})
                raise AttemptBudgetRejectedError(
                    error_code=str(error.get("code", "budget_exhausted")),
                    error_message=str(
                        error.get("message", "Conversation LLM budget exhausted.")
                    ),
                )
            step = adapter.select_step(request)
            handle_selected_step(step)
            if step.delay_ms > 0:
                advance_with_pump(step.delay_ms)
            if stop_event.is_set() or state.paused or state.active_run_id is None:
                finalize_attempt(
                    run_id,
                    attempt_id,
                    status="cancelled",
                    error_code="interrupted",
                    error_message="Attempt interrupted before completion.",
                    error_class="interrupted",
                    prompt_tokens=len(haystack),
                    completion_tokens=0,
                    total_tokens=len(haystack),
                )
                raise PhaseExecutionError(
                    error_code="interrupted",
                    error_message="Attempt interrupted before completion.",
                )
            if step.fail_code is not None:
                error_class, error_message, retryable, backoff_ms = classify_failure(
                    step.fail_code,
                    retry_index=retry_index,
                )
                finalize_attempt(
                    run_id,
                    attempt_id,
                    status="failed",
                    payload={"fail_code": step.fail_code},
                    error_code=step.fail_code.lower(),
                    error_message=error_message,
                    error_class=error_class,
                    retryable=retryable,
                    backoff_ms=backoff_ms,
                    prompt_tokens=len(haystack),
                    completion_tokens=0,
                    total_tokens=len(haystack),
                )
                if retryable:
                    retry_index += 1
                    if backoff_ms > 0:
                        advance_with_pump(backoff_ms)
                        if stop_event.is_set() or state.paused or state.active_run_id is None:
                            raise PhaseExecutionError(
                                error_code="interrupted",
                                error_message="Attempt interrupted during retry backoff.",
                            )
                    continue
                raise PhaseExecutionError(
                    error_code=step.fail_code.lower(),
                    error_message=error_message,
                )
            payload = step.output_json or fallback_payload or {}
            output_text = serialized_output(step)
            prompt_tokens = len(haystack)
            completion_tokens = len(output_text)
            finalize_attempt(
                run_id,
                attempt_id,
                status="succeeded",
                payload=payload,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )
            return payload

    def process_runtime_event(event: dict[str, Any]) -> None:
        event_type = event.get("event_type")
        if event_type == "conversation.state_changed":
            next_state = event["payload"]["state"]
            state.paused = next_state == "paused"
            state.pause_requested = next_state == "paused"
            if next_state == "ended":
                stop_event.set()
                return
            if next_state == "paused":
                set_typing(False, state.active_run_id, "paused")
                if state.active_run_id is not None:
                    post_status("INVALIDATED", "PAUSED", state.active_run_id)
                    state.active_run_id = None
                post_worker_state("PAUSED")
                state.pause_requested = False
                return
            if next_state == "running":
                post_worker_state("LISTENING")
            return
        if event_type != "message.committed":
            return
        message = event["payload"]["message"]
        if int(message["conversation_seq"]) > state.reliable_seq:
            state.reliable_seq = int(message["conversation_seq"])
        if should_queue(runtime_payload, state, config.agent_id, message):
            state.pending_messages.append(message)
            if message["sender_kind"] == "user":
                state.pending_roots.append(message["message_id"])
        if state.active_run_id is not None and message["sender_id"] != config.agent_id:
            state.dirty_since_seq = int(message["conversation_seq"])

    def pump_events(timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                event = incoming.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                return
            process_runtime_event(event)

    def advance_with_pump(delay_ms: int) -> None:
        deadline = time.monotonic() + (delay_ms / 1000)
        while (
            not stop_event.is_set()
            and time.monotonic() < deadline
            and not state.paused
            and state.active_run_id is not None
        ):
            remaining = max(0.0, deadline - time.monotonic())
            try:
                event = incoming.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            process_runtime_event(event)

    while not stop_event.is_set():
        pump_events(0.05)

        if (
            stop_event.is_set()
            or state.paused
            or state.active_run_id is not None
            or not state.pending_messages
        ):
            continue

        current_batch = list(state.pending_messages)
        state.pending_messages.clear()
        if state.pending_roots:
            root_ids = set(state.pending_roots)
            root_message = next(
                (message for message in current_batch if message["message_id"] in root_ids),
                None,
            )
            if root_message is not None:
                deferred_messages = [
                    message
                    for message in current_batch
                    if message["message_id"] != root_message["message_id"]
                ]
                if deferred_messages:
                    state.pending_messages = deferred_messages + state.pending_messages
                current_batch = [root_message]
                state.pending_roots = [str(root_message["message_id"])]
        current_ids = {str(msg["message_id"]) for msg in current_batch}
        state.pending_roots = [item for item in state.pending_roots if item in current_ids]
        latest = current_batch[-1]
        mentions = set(latest["mentions"])
        all_mention = mentions.issuperset(runtime_payload["all_agent_ids"])
        direct_mention = config.agent_id in mentions
        state.current_all_mention = all_mention
        state.current_direct_mention = direct_mention
        state.active_run_id = f"run-{secrets.token_hex(10)}"
        latest_seq = int(latest["conversation_seq"])
        if is_root_stage_peer_handoff(state, latest):
            state.expected_conversation_seq = max(state.reliable_seq, latest_seq)
        else:
            state.expected_conversation_seq = latest_seq
        state.caused_by_message_id = latest["message_id"]
        if state.pending_roots:
            root_digest = sha1("|".join(state.pending_roots).encode("utf-8")).hexdigest()
            state.causal_episode_id = f"episode-{root_digest[:16]}"
            state.agent_hop = 0
            if state.caused_by_message_id is not None:
                state.episode_root_message_ids[state.causal_episode_id] = state.caused_by_message_id
        else:
            state.causal_episode_id = latest.get("causal_episode_id")
            state.agent_hop = int(latest.get("agent_hop") or 0) + 1

        runtime_payload = fetch_runtime_snapshot(client, config)
        run_guardrails = guardrails_snapshot(runtime_payload)
        current_memory = runtime_store.snapshot()
        recent_messages = fetch_recent_messages(
            client,
            config,
            after_seq=max(0, latest_seq - 30),
        )
        decision_bundle = runtime_store.build_context_bundle(
            phase="decision",
            as_of_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            conversation_seq=latest_seq,
            projection_revision=int(runtime_payload["latest_cp_revision"]),
            cp_snapshot=dict(runtime_payload["cp_snapshot"]),
            memory_snapshot=current_memory,
            public_messages=recent_messages,
            observation_message_ids=[str(item["message_id"]) for item in current_batch],
        )
        post_status(
            "RUNNING",
            "DECIDING",
            state.active_run_id,
            extra={
                "decision_json": build_context_trace(
                    bundle=decision_bundle,
                    memory_revision_before=current_memory["revision"],
                    staged_memory_revision=None,
                    run_guardrails=run_guardrails,
                )
            },
        )
        if direct_mention:
            set_typing(True, state.active_run_id, "decision")
        decision_request_payload = decision_request(config.agent_id, latest)
        try:
            decision_payload = run_model_phase(
                phase="decision",
                request=decision_request_payload,
                bundle_revision=str(decision_bundle["revision"]),
                memory_revision_before=str(current_memory["revision"]),
                staged_memory_revision=None,
                fallback_payload={},
            )
        except AttemptBudgetRejectedError as exc:
            post_status(
                "BUDGET_EXHAUSTED",
                "DECIDING",
                state.active_run_id,
                extra={
                    "error_code": exc.error_code,
                    "error_message": exc.error_message,
                },
            )
            set_typing(False, state.active_run_id, "budget-exhausted")
            state.active_run_id = None
            state.expected_conversation_seq = None
            state.dirty_since_seq = None
            state.pending_roots.clear()
            post_worker_state("LISTENING")
            continue
        except PhaseExecutionError as exc:
            if state.active_run_id is not None:
                post_status(
                    "FATAL",
                    "DECIDING",
                    state.active_run_id,
                    extra={
                        "error_code": exc.error_code,
                        "error_message": exc.error_message,
                    },
                )
                set_typing(False, state.active_run_id, "fatal")
                state.active_run_id = None
                state.expected_conversation_seq = None
                state.dirty_since_seq = None
                state.pending_roots.clear()
                post_worker_state("LISTENING")
            continue
        if stop_event.is_set() or state.paused or state.active_run_id is None:
            continue
        decision_operations = normalize_memory_operations(decision_payload)
        decision_stage = runtime_store.stage_preview(
            operations=decision_operations,
            run_id=state.active_run_id,
            phase="decision",
            as_of_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        should_reply = bool(decision_payload.get("should_reply", direct_mention or all_mention))
        if direct_mention or all_mention:
            should_reply = True
        decision_trace = build_context_trace(
            bundle=decision_bundle,
            memory_revision_before=current_memory["revision"],
            staged_memory_revision=(
                None if decision_stage is None else str(decision_stage["revision"])
            ),
            run_guardrails=run_guardrails,
            should_reply=should_reply,
            payload=decision_payload,
        )
        if not should_reply:
            post_status(
                "SILENT",
                "DECIDING",
                state.active_run_id,
                extra={"decision_json": decision_trace},
            )
            set_typing(False, state.active_run_id, "silent")
            state.active_run_id = None
            state.dirty_since_seq = None
            post_worker_state("LISTENING")
            continue

        action_bundle = runtime_store.build_context_bundle(
            phase="action",
            as_of_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            conversation_seq=latest_seq,
            projection_revision=int(runtime_payload["latest_cp_revision"]),
            cp_snapshot=dict(runtime_payload["cp_snapshot"]),
            memory_snapshot=current_memory,
            public_messages=recent_messages,
            observation_message_ids=[str(item["message_id"]) for item in current_batch],
            decision_payload=decision_payload,
        )
        if not direct_mention:
            set_typing(True, state.active_run_id, "action")
        action_request_payload = action_request(config.agent_id, decision_payload)
        try:
            draft = run_model_phase(
                phase="action",
                request=action_request_payload,
                bundle_revision=str(action_bundle["revision"]),
                memory_revision_before=str(current_memory["revision"]),
                staged_memory_revision=(
                    None if decision_stage is None else str(decision_stage["revision"])
                ),
                fallback_payload={
                    "content_markdown": "ACK",
                    "mentions": [],
                    "primary_reply_to": state.caused_by_message_id,
                    "responds_to": (
                        [state.caused_by_message_id] if state.caused_by_message_id else []
                    ),
                },
            )
        except AttemptBudgetRejectedError as exc:
            post_status(
                "BUDGET_EXHAUSTED",
                "ACTING",
                state.active_run_id,
                extra={
                    "decision_json": decision_trace,
                    "error_code": exc.error_code,
                    "error_message": exc.error_message,
                },
            )
            set_typing(False, state.active_run_id, "budget-exhausted")
            state.active_run_id = None
            state.expected_conversation_seq = None
            state.dirty_since_seq = None
            state.pending_roots.clear()
            post_worker_state("LISTENING")
            continue
        except PhaseExecutionError as exc:
            if state.active_run_id is not None:
                post_status(
                    "FATAL",
                    "ACTING",
                    state.active_run_id,
                    extra={
                        "decision_json": decision_trace,
                        "error_code": exc.error_code,
                        "error_message": exc.error_message,
                    },
                )
                set_typing(False, state.active_run_id, "fatal")
                state.active_run_id = None
                state.expected_conversation_seq = None
                state.dirty_since_seq = None
                state.pending_roots.clear()
                post_worker_state("LISTENING")
            continue
        if stop_event.is_set() or state.paused or state.active_run_id is None:
            continue
        combined_operations = decision_operations + normalize_memory_operations(draft)
        staged_combined = runtime_store.stage_preview(
            operations=combined_operations,
            run_id=state.active_run_id,
            phase="action",
            as_of_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        action_trace = build_context_trace(
            bundle=action_bundle,
            memory_revision_before=current_memory["revision"],
            staged_memory_revision=(
                None if staged_combined is None else str(staged_combined["revision"])
            ),
            run_guardrails=run_guardrails,
            payload={
                key: value for key, value in draft.items() if key != "memory_delta"
            },
        )

        wait_ms = timing_wait_ms(
            runtime_payload,
            direct_mention=direct_mention,
            content=str(draft["content_markdown"]),
        )
        if wait_ms > 0:
            advance_with_pump(wait_ms)
        if (
            stop_event.is_set()
            or state.paused
            or state.active_run_id is None
            or (
                state.dirty_since_seq is not None
                and state.dirty_since_seq > (state.expected_conversation_seq or 0)
                and not all_mention
            )
        ):
            post_status(
                "INVALIDATED",
                "RECONSIDER_BEFORE_SEND",
                state.active_run_id,
                extra={
                    "decision_json": decision_trace,
                    "draft_message_json": action_trace,
                },
            )
            set_typing(False, state.active_run_id, "invalidated")
            state.active_run_id = None
            post_worker_state("LISTENING")
            continue

        while True:
            response = client.post(
                f"{config.server_base_url}/internal/v1/conversations/{config.conversation_id}/agent-actions",
                headers=auth_headers(config),
                json={
                    "run_id": state.active_run_id,
                    "expected_conversation_seq": state.expected_conversation_seq,
                    "idempotency_key": state.active_run_id,
                    "content_markdown": draft["content_markdown"],
                    "mentions": draft.get("mentions", []),
                    "primary_reply_to": draft.get("primary_reply_to"),
                    "responds_to": draft.get("responds_to", []),
                    "causal_episode_id": state.causal_episode_id,
                    "caused_by_message_id": state.caused_by_message_id,
                    "agent_hop": state.agent_hop,
                },
            )
            if response.status_code == 409:
                error = response.json()["error"]
                if error["code"] == "stale_sequence" and all_mention:
                    details = error["details"]
                    state.expected_conversation_seq = int(details["actual_conversation_seq"])
                    continue
                if error["code"] == "stale_sequence":
                    post_status(
                        "INVALIDATED",
                        "RECONSIDER_BEFORE_SEND",
                        state.active_run_id,
                        extra={
                            "decision_json": decision_trace,
                            "draft_message_json": action_trace,
                        },
                    )
                    set_typing(False, state.active_run_id, "stale-sequence")
                    state.active_run_id = None
                    post_worker_state("LISTENING")
                    break
                if error["code"] == "budget_exhausted":
                    post_status(
                        "BUDGET_EXHAUSTED",
                        "SUBMITTING",
                        state.active_run_id,
                        extra={
                            "decision_json": decision_trace,
                            "draft_message_json": action_trace,
                        },
                    )
                    set_typing(False, state.active_run_id, "budget-exhausted")
                    state.active_run_id = None
                    post_worker_state("LISTENING")
                    break
            response.raise_for_status()
            break

        if state.active_run_id is None:
            state.expected_conversation_seq = None
            state.dirty_since_seq = None
            state.pending_roots.clear()
            continue

        response_payload = response.json()
        committed_message = response_payload["message"]
        cp_revision = response_payload["cp_revision"]
        state.reliable_seq = int(committed_message["conversation_seq"])
        memory_after = runtime_store.commit_operations(
            operations=combined_operations,
            run_id=state.active_run_id,
            conversation_seq=int(committed_message["conversation_seq"]),
            cp_revision=int(cp_revision["projection_revision"]),
            bundle_revisions=[decision_bundle["revision"], action_bundle["revision"]],
            as_of_time=str(committed_message["committed_at"]),
        )
        decision_trace["memory_revision_after"] = memory_after["revision"]
        action_trace["memory_revision_after"] = memory_after["revision"]
        post_status(
            "COMMITTED",
            "SUBMITTING",
            state.active_run_id,
            extra={
                "decision_json": decision_trace,
                "draft_message_json": action_trace,
            },
        )
        set_typing(False, state.active_run_id, "submitted")
        state.active_run_id = None
        state.expected_conversation_seq = None
        state.dirty_since_seq = None
        state.pending_roots.clear()
        post_worker_state("LISTENING")

    if state.active_run_id is not None:
        post_status("INVALIDATED", "SHUTDOWN", state.active_run_id)
        state.active_run_id = None
    set_typing(False, state.active_run_id, "shutdown")
    ws.close()


if __name__ == "__main__":
    run_worker()
