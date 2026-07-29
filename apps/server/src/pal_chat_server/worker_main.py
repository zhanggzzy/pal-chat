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
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from pal_chat_server.contracts import ModelRequest
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
    current_draft: dict[str, Any] | None = None
    current_all_mention: bool = False
    current_direct_mention: bool = False
    caused_by_message_id: str | None = None
    causal_episode_id: str | None = None
    agent_hop: int = 0
    paused: bool = False
    typing_active: bool = False


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
        if not isinstance(decoded, dict):
            return {}
        return decoded

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
    return [
        ScriptedStep.model_validate(item)
        for item in adapter_config.get("script", [])
    ]


def timing_wait_ms(payload: dict[str, Any], *, direct_mention: bool, content: str) -> int:
    config = payload["profile"]["modules"]["timing"]["config"]
    base_ms = int(config.get("base_wait_ms", 0))
    per_char_ms = int(config.get("per_char_wait_ms", 0))
    minimum_typing_ms = int(config.get("minimum_typing_ms", 0))
    wait_ms = base_ms + len(content) * per_char_ms
    return max(minimum_typing_ms, wait_ms) if direct_mention else max(0, wait_ms)


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
    return agent_id in mentions


def run_worker() -> None:
    config = env_config()
    client = httpx.Client(timeout=10)
    stop_event = threading.Event()

    def stop_handler(*_: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    snapshot = client.get(
        f"{config.server_base_url}/internal/v1/conversations/{config.conversation_id}/runtime-snapshot",
        headers=auth_headers(config),
    )
    snapshot.raise_for_status()
    runtime_payload = snapshot.json()
    script = select_script(runtime_payload)
    adapter = ScriptedModelAdapter(script)
    state = WorkerState(reliable_seq=int(runtime_payload["latest_conversation_seq"]))
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

    def post_status(status: str, phase: str, run_id: str | None = None) -> None:
        run_segment = run_id or "-"
        client.post(
            f"{config.server_base_url}/internal/v1/conversations/"
            f"{config.conversation_id}/agent-runs/{run_segment}/status",
            headers=auth_headers(config),
            json={
                "worker_state": phase,
                "reliable_seq": state.reliable_seq,
                "dirty_since_seq": state.dirty_since_seq,
                "active_run_id": run_id,
                "run_status": status,
                "typing_action": "start" if state.typing_active else "stop",
            },
        ).raise_for_status()

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

    while not stop_event.is_set():
        while True:
            try:
                event = incoming.get(timeout=0.05)
            except queue.Empty:
                break
            event_type = event.get("event_type")
            if event_type == "conversation.state_changed":
                state.paused = event["payload"]["state"] == "paused"
                if event["payload"]["state"] == "ended":
                    stop_event.set()
                    break
                continue
            if event_type != "message.committed":
                continue
            message = event["payload"]["message"]
            if int(message["conversation_seq"]) > state.reliable_seq:
                state.reliable_seq = int(message["conversation_seq"])
            if should_queue(runtime_payload, state, config.agent_id, message):
                state.pending_messages.append(message)
                if message["sender_kind"] == "user":
                    state.pending_roots.append(message["message_id"])
            if state.active_run_id is not None and message["sender_id"] != config.agent_id:
                state.dirty_since_seq = int(message["conversation_seq"])

        if (
            stop_event.is_set()
            or state.paused
            or state.active_run_id is not None
            or not state.pending_messages
        ):
            continue

        current_batch = list(state.pending_messages)
        state.pending_messages.clear()
        current_ids = {str(msg["message_id"]) for msg in current_batch}
        state.pending_roots = [
            item for item in state.pending_roots if item in current_ids
        ]
        latest = current_batch[-1]
        mentions = set(latest["mentions"])
        all_mention = mentions.issuperset(runtime_payload["all_agent_ids"])
        direct_mention = config.agent_id in mentions
        state.current_all_mention = all_mention
        state.current_direct_mention = direct_mention
        state.active_run_id = f"run-{secrets.token_hex(10)}"
        state.expected_conversation_seq = int(latest["conversation_seq"])
        state.caused_by_message_id = latest["message_id"]
        if state.pending_roots:
            root_digest = sha1("|".join(state.pending_roots).encode("utf-8")).hexdigest()
            state.causal_episode_id = f"episode-{root_digest[:16]}"
            state.agent_hop = 0
        else:
            state.causal_episode_id = latest.get("causal_episode_id")
            state.agent_hop = int(latest.get("agent_hop") or 0) + 1
        post_status("RUNNING", "DECIDING", state.active_run_id)
        if direct_mention:
            set_typing(True, state.active_run_id, "decision")
        selected = adapter.select_step(
            decision_request(config.agent_id, latest),
        )
        if selected.fail_code == "WORKER_CRASHED":
            os._exit(90)
        if selected.fail_code == "WORKER_CRASHED_FATAL":
            os._exit(91)
        decision_payload = selected.output_json or json.loads(
            selected.output_text or '{"should_reply": false}'
        )
        should_reply = bool(decision_payload.get("should_reply", direct_mention or all_mention))
        if direct_mention or all_mention:
            should_reply = True
        if not should_reply:
            post_status("SILENT", "DECIDING", state.active_run_id)
            set_typing(False, state.active_run_id, "silent")
            state.active_run_id = None
            state.dirty_since_seq = None
            continue
        action_step = adapter.select_step(
            action_request(config.agent_id, decision_payload),
        )
        if not direct_mention:
            set_typing(True, state.active_run_id, "action")
        if action_step.fail_code == "WORKER_CRASHED":
            os._exit(90)
        if action_step.fail_code == "WORKER_CRASHED_FATAL":
            os._exit(91)
        draft = action_step.output_json or {
            "content_markdown": action_step.output_text or "ACK",
            "mentions": [],
            "primary_reply_to": state.caused_by_message_id,
            "responds_to": [state.caused_by_message_id] if state.caused_by_message_id else [],
        }
        wait_ms = timing_wait_ms(
            runtime_payload,
            direct_mention=direct_mention,
            content=str(draft["content_markdown"]),
        )
        if wait_ms > 0:
            time.sleep(wait_ms / 1000)
        if (
            state.dirty_since_seq is not None
            and state.dirty_since_seq > (state.expected_conversation_seq or 0)
            and not all_mention
        ):
            post_status("INVALIDATED", "RECONSIDER_BEFORE_SEND", state.active_run_id)
            set_typing(False, state.active_run_id, "invalidated")
            state.active_run_id = None
            state.current_draft = None
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
            if (
                response.status_code == 409
                and response.json()["error"]["code"] == "stale_sequence"
                and all_mention
            ):
                details = response.json()["error"]["details"]
                state.expected_conversation_seq = int(
                    details["actual_conversation_seq"]
                )
                continue
            response.raise_for_status()
            break
        post_status("COMMITTED", "SUBMITTING", state.active_run_id)
        set_typing(False, state.active_run_id, "submitted")
        state.active_run_id = None
        state.expected_conversation_seq = None
        state.dirty_since_seq = None
        state.pending_roots.clear()

    set_typing(False, state.active_run_id, "shutdown")
    ws.close()


if __name__ == "__main__":
    run_worker()
