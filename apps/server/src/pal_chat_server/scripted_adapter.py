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

    def _effective_call_index(self, request: ModelRequest) -> int:
        hinted = request.metadata.get("call_index")
        if hinted is not None:
            return int(hinted)
        self._call_count += 1
        return self._call_count

    def _default_step(self, request: ModelRequest, *, haystack: str) -> ScriptedStep:
        purpose = request.purpose
        metadata = request.metadata
        if purpose == "decision":
            return ScriptedStep(
                output_json={"should_reply": False, "reason_codes": []},
            )
        if purpose == "action":
            return ScriptedStep(
                output_json={
                    "content_markdown": "ACK",
                    "mentions": [],
                    "primary_reply_to": metadata.get("primary_reply_to"),
                    "responds_to": metadata.get("responds_to", []),
                }
            )
        if purpose == "projection":
            has_open_segment = bool(metadata.get("has_open_segment"))
            return ScriptedStep(
                output_json={
                    "operation": "APPEND_CURRENT" if has_open_segment else "START_NEW_SEGMENT",
                    "segment_title": str(
                        metadata.get("default_segment_title", "公共消息")
                    ),
                    "updated_summary": haystack[:140],
                }
            )
        if purpose == "reconsideration":
            return ScriptedStep(
                output_json={"should_continue": False, "reason": "stale"}
            )
        if purpose == "repair":
            fallback_payload = metadata.get("fallback_payload")
            if isinstance(fallback_payload, dict):
                return ScriptedStep(output_json=fallback_payload)
            return ScriptedStep(output_json={})
        return ScriptedStep(output_text="ACK")

    def select_step(self, request: ModelRequest) -> ScriptedStep:
        call_index = self._effective_call_index(request)
        haystack = "\n".join(message["content"] for message in request.messages)
        agent_id = str(request.metadata.get("agent_id", ""))
        selected = next(
            (
                step
                for step in self.steps
                if (step.purpose is None or step.purpose == request.purpose)
                and (step.agent_id is None or step.agent_id == agent_id)
                and (step.call_index is None or step.call_index == call_index)
                and (step.match_contains is None or step.match_contains in haystack)
            ),
            None,
        )
        if selected is None:
            return self._default_step(request, haystack=haystack)
        return selected

    async def complete(self, request: ModelRequest) -> ModelResponse:
        haystack = "\n".join(message["content"] for message in request.messages)
        selected = self.select_step(request)
        call_index = int(request.metadata.get("call_index", self._call_count))
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
                "call_index": call_index,
            },
        )
