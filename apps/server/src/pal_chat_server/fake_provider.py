from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ModelResult:
    provider: str
    model: str
    output: dict[str, Any]
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    latency_ms: int
    request_id: str


class ModelGateway(Protocol):
    async def classify_topic(self, request: dict[str, Any]) -> ModelResult: ...
    async def score_participation(self, request: dict[str, Any]) -> ModelResult: ...
    async def generate_reply(self, request: dict[str, Any]) -> ModelResult: ...
    async def summarize_topic(self, request: dict[str, Any]) -> ModelResult: ...


class FakeProviderError(RuntimeError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


@dataclass(slots=True)
class FakeBehavior:
    output: dict[str, Any] = field(default_factory=dict)
    delay_ms: int = 0
    bad_json: bool = False
    error_category: str | None = None
    error_message: str = "fake provider error"
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class FakeModelGateway:
    def __init__(
        self,
        *,
        provider: str = "fake",
        model: str = "fake-model",
        behaviors: dict[str, FakeBehavior] | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.behaviors = behaviors or {}

    async def classify_topic(self, request: dict[str, Any]) -> ModelResult:
        return await self._execute("classify_topic", request)

    async def score_participation(self, request: dict[str, Any]) -> ModelResult:
        return await self._execute("score_participation", request)

    async def generate_reply(self, request: dict[str, Any]) -> ModelResult:
        return await self._execute("generate_reply", request)

    async def summarize_topic(self, request: dict[str, Any]) -> ModelResult:
        return await self._execute("summarize_topic", request)

    async def _execute(self, operation: str, request: dict[str, Any]) -> ModelResult:
        behavior = self.behaviors.get(operation, FakeBehavior(output=request))
        started = time.perf_counter()
        if behavior.delay_ms:
            await asyncio.sleep(behavior.delay_ms / 1000)
        if behavior.error_category:
            raise FakeProviderError(behavior.error_category, behavior.error_message)
        if behavior.bad_json:
            json.loads("{bad json")
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ModelResult(
            provider=self.provider,
            model=self.model,
            output=behavior.output,
            input_tokens=behavior.input_tokens,
            output_tokens=behavior.output_tokens,
            cached_tokens=behavior.cached_tokens,
            latency_ms=latency_ms,
            request_id=f"{operation}-request",
        )
