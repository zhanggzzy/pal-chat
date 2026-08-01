from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from pal_chat_server.clock import VirtualClock
from pal_chat_server.contracts import ModelAdapter, ModelRequest
from pal_chat_server.ids import generate_ulid
from pal_chat_server.scripted_adapter import ScriptedModelAdapter, ScriptedStep


def test_ulid_is_fixed_length() -> None:
    value = generate_ulid(datetime(2026, 7, 29, tzinfo=UTC))
    assert len(value) == 26


def test_virtual_clock_advances_monotonic_time() -> None:
    clock = VirtualClock(current=datetime(2026, 7, 29, tzinfo=UTC))
    clock.advance(timedelta(seconds=3))
    assert clock.now_utc() == datetime(2026, 7, 29, 0, 0, 3, tzinfo=UTC)
    assert clock.monotonic_ns() == 3_000_000_000


def test_scripted_adapter_matches_rule_without_sleeping_real_time() -> None:
    clock = VirtualClock(current=datetime(2026, 7, 29, tzinfo=UTC))
    adapter = ScriptedModelAdapter(
        [ScriptedStep(match_contains="hello", output_text="world", delay_ms=150)],
        clock=clock,
    )
    assert isinstance(adapter, ModelAdapter)
    response = asyncio.run(
        adapter.complete(
            ModelRequest(
                purpose="decision",
                system_prompt="test",
                messages=[{"role": "user", "content": "hello"}],
            )
        )
    )
    assert response.output_text == "world"
    assert clock.monotonic_ns() == 150_000_000
