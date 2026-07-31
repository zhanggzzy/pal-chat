from __future__ import annotations

import threading
from contextvars import ContextVar
from time import perf_counter
from typing import Any

_CURRENT_TRACE_ID: ContextVar[str | None] = ContextVar("pal_chat_perf_trace_id", default=None)
_TRACE_STORE: dict[str, dict[str, Any]] = {}
_CONVERSATION_TRACES: dict[str, str] = {}
_LOCK = threading.Lock()
_MAX_TRACES = 256


def start_trace(*, trace_id: str, method: str, path: str) -> None:
    _CURRENT_TRACE_ID.set(trace_id)
    with _LOCK:
        _TRACE_STORE[trace_id] = {
            "trace_id": trace_id,
            "method": method,
            "path": path,
            "sync_segments": [],
            "dispatcher_segments": [],
            "metadata": {},
        }
        while len(_TRACE_STORE) > _MAX_TRACES:
            oldest = next(iter(_TRACE_STORE))
            _TRACE_STORE.pop(oldest, None)


def current_trace_id() -> str | None:
    return _CURRENT_TRACE_ID.get()


def trace_enabled() -> bool:
    return current_trace_id() is not None


def end_trace(*, total_ms: float) -> None:
    trace_id = current_trace_id()
    if trace_id is None:
        return
    with _LOCK:
        trace = _TRACE_STORE.get(trace_id)
        if trace is None:
            return
        sync_total = sum(float(item["duration_ms"]) for item in trace["sync_segments"])
        trace["total_request_ms"] = round(total_ms, 3)
        trace["response_encode_ms"] = round(max(total_ms - sync_total, 0.0), 3)


def set_metadata(key: str, value: Any) -> None:
    trace_id = current_trace_id()
    if trace_id is None:
        return
    with _LOCK:
        trace = _TRACE_STORE.get(trace_id)
        if trace is not None:
            trace["metadata"][key] = value


def register_conversation_trace(conversation_id: str) -> None:
    trace_id = current_trace_id()
    if trace_id is None:
        return
    with _LOCK:
        _CONVERSATION_TRACES[conversation_id] = trace_id


def append_sync_segment(name: str, duration_ms: float, **extra: Any) -> None:
    trace_id = current_trace_id()
    if trace_id is None:
        return
    with _LOCK:
        trace = _TRACE_STORE.get(trace_id)
        if trace is None:
            return
        trace["sync_segments"].append(
            {
                "name": name,
                "duration_ms": round(duration_ms, 3),
                **extra,
            }
        )


def append_dispatcher_segment(
    conversation_id: str,
    name: str,
    duration_ms: float,
    **extra: Any,
) -> None:
    with _LOCK:
        trace_id = _CONVERSATION_TRACES.get(conversation_id)
        trace = None if trace_id is None else _TRACE_STORE.get(trace_id)
        if trace is None:
            return
        trace["dispatcher_segments"].append(
            {
                "name": name,
                "duration_ms": round(duration_ms, 3),
                **extra,
            }
        )
        if name == "dispatcher.completed":
            _CONVERSATION_TRACES.pop(conversation_id, None)


def get_trace(trace_id: str) -> dict[str, Any] | None:
    with _LOCK:
        trace = _TRACE_STORE.get(trace_id)
        if trace is None:
            return None
        return {
            "trace_id": trace["trace_id"],
            "method": trace["method"],
            "path": trace["path"],
            "sync_segments": list(trace["sync_segments"]),
            "dispatcher_segments": list(trace["dispatcher_segments"]),
            "metadata": dict(trace["metadata"]),
            "total_request_ms": trace.get("total_request_ms"),
            "response_encode_ms": trace.get("response_encode_ms"),
        }


def timed_segment(name: str) -> _TimedSegment:
    return _TimedSegment(name)


class _TimedSegment:
    def __init__(self, name: str) -> None:
        self._name = name
        self._started = 0.0

    def __enter__(self) -> _TimedSegment:
        self._started = perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        append_sync_segment(self._name, (perf_counter() - self._started) * 1000)
