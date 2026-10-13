from __future__ import annotations

import json
import time

import pytest
from pal_chat_server.fake_provider import (
    FakeBehavior,
    FakeModelGateway,
    FakeProviderError,
)


@pytest.mark.anyio
async def test_fake_provider_returns_programmed_output() -> None:
    gateway = FakeModelGateway(
        behaviors={
            "generate_reply": FakeBehavior(
                output={"reply": "hello"},
                input_tokens=11,
                output_tokens=7,
                cached_tokens=1,
            )
        }
    )

    result = await gateway.generate_reply({"prompt": "hi"})
    assert result.output == {"reply": "hello"}
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.cached_tokens == 1


@pytest.mark.anyio
async def test_fake_provider_supports_delay() -> None:
    gateway = FakeModelGateway(
        behaviors={"classify_topic": FakeBehavior(output={"action": "create"}, delay_ms=25)}
    )

    started = time.perf_counter()
    await gateway.classify_topic({"message": "x"})
    assert (time.perf_counter() - started) >= 0.02


@pytest.mark.anyio
async def test_fake_provider_can_raise_rate_limit() -> None:
    gateway = FakeModelGateway(
        behaviors={"score_participation": FakeBehavior(error_category="rate_limit")}
    )
    with pytest.raises(FakeProviderError) as exc_info:
        await gateway.score_participation({"message": "x"})
    assert exc_info.value.category == "rate_limit"


@pytest.mark.anyio
async def test_fake_provider_can_emit_bad_json() -> None:
    gateway = FakeModelGateway(behaviors={"summarize_topic": FakeBehavior(bad_json=True)})
    with pytest.raises(json.JSONDecodeError):
        await gateway.summarize_topic({"topic": "x"})
