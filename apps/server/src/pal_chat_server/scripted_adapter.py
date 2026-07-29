from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict

from pal_chat_server.clock import Clock, VirtualClock
from pal_chat_server.contracts import ModelRequest, ModelResponse


class ScriptedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str | None = None
    agent_id: str | None = None
    call_index: int | None = None
    match_contains: str | None = None
    output_text: str = ""
    output_json: dict[str, Any] | None = None
    delay_ms: int = 0
    finish_reason: str = "stop"
    fail_code: str | None = None


class ScriptedAdapterError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ScriptedModelAdapter:
    def __init__(self, steps: list[ScriptedStep], clock: Clock | None = None) -> None:
        self.steps = steps
        self.clock = clock
        self._call_count = 0

    def select_step(self, request: ModelRequest) -> ScriptedStep:
        self._call_count += 1
        haystack = "\n".join(message["content"] for message in request.messages)
        agent_id = str(request.metadata.get("agent_id", ""))
        selected = next(
            (
                step
                for step in self.steps
                if (step.purpose is None or step.purpose == request.purpose)
                and (step.agent_id is None or step.agent_id == agent_id)
                and (step.call_index is None or step.call_index == self._call_count)
                and (step.match_contains is None or step.match_contains in haystack)
            ),
            None,
        )
        if selected is None:
            return ScriptedStep(output_text="ACK")
        return selected

    async def complete(self, request: ModelRequest) -> ModelResponse:
        haystack = "\n".join(message["content"] for message in request.messages)
        selected = self.select_step(request)
        if selected.delay_ms > 0:
            if isinstance(self.clock, VirtualClock):
                self.clock.advance(timedelta(milliseconds=selected.delay_ms))
            else:
                await asyncio.sleep(selected.delay_ms / 1000)
        if selected.fail_code is not None:
            raise ScriptedAdapterError(selected.fail_code)
        output_text = (
            json.dumps(selected.output_json, ensure_ascii=False)
            if selected.output_json is not None
            else selected.output_text
        )
        return ModelResponse(
            output_text=output_text,
            finish_reason=selected.finish_reason,
            usage={"input_tokens": len(haystack), "output_tokens": len(output_text)},
            raw_payload={
                "adapter": "scripted",
                "matched": selected.match_contains,
                "purpose": selected.purpose,
                "agent_id": selected.agent_id,
                "call_index": self._call_count,
            },
        )
