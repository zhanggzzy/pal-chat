from __future__ import annotations

import asyncio
from datetime import timedelta

from pydantic import BaseModel, ConfigDict

from pal_chat_server.clock import Clock, VirtualClock
from pal_chat_server.contracts import ModelRequest, ModelResponse


class ScriptedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    match_contains: str | None = None
    output_text: str
    delay_ms: int = 0
    finish_reason: str = "stop"


class ScriptedModelAdapter:
    def __init__(self, steps: list[ScriptedStep], clock: Clock | None = None) -> None:
        self.steps = steps
        self.clock = clock

    async def complete(self, request: ModelRequest) -> ModelResponse:
        haystack = "\n".join(message["content"] for message in request.messages)
        selected = next(
            (
                step
                for step in self.steps
                if step.match_contains is None or step.match_contains in haystack
            ),
            None,
        )
        if selected is None:
            selected = ScriptedStep(output_text="ACK")
        if selected.delay_ms > 0:
            if isinstance(self.clock, VirtualClock):
                self.clock.advance(timedelta(milliseconds=selected.delay_ms))
            else:
                await asyncio.sleep(selected.delay_ms / 1000)
        return ModelResponse(
            output_text=selected.output_text,
            finish_reason=selected.finish_reason,
            usage={"input_tokens": len(haystack), "output_tokens": len(selected.output_text)},
            raw_payload={"adapter": "scripted", "matched": selected.match_contains},
        )
