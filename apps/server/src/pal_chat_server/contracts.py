from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class CommitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    expected_conversation_seq: int | None = None
    message_markdown: str = ""
    idempotency_key: str | None = None


class CommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_seq: int
    committed_message_id: str
    cp_revision: int


class PublicMessageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    conversation_id: str
    conversation_seq: int
    content_markdown: str


class ObservationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_message_ids: list[str]
    direct_mentions: list[str] = Field(default_factory=list)


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    cp_revision: int
    memory_revision: str
    observation_message_ids: list[str]


class ContextBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    rendered_items: list[str]
    token_estimate: int = 0


class MemorySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str
    payload: dict[str, Any]


class MemoryDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[dict[str, Any]]


class StagedMemoryRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str
    payload: dict[str, Any]


class InspectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str
    visualization_kind: str
    payload: dict[str, Any]


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_reply: bool
    reason_codes: list[str] = Field(default_factory=list)


class ReconsiderationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_seq: int
    stale_after_seq: int


class ReconsiderationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    should_continue: bool
    reason: str


class TimingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    agent_id: str
    latest_seq: int


class TimingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wait_ms: int
    jitter_applied_ms: int = 0


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    system_prompt: str
    messages: list[dict[str, str]]
    max_tokens: int = 512
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_text: str
    finish_reason: str = "stop"
    usage: dict[str, int] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ConversationCommitter(Protocol):
    async def commit(self, request: CommitRequest) -> CommitResult: ...


@runtime_checkable
class TriggerStrategy(Protocol):
    def ingest(self, event: PublicMessageEvent) -> list[ObservationBatch]: ...


@runtime_checkable
class ContextAssemblyStrategy(Protocol):
    async def build(self, request: ContextRequest) -> ContextBundle: ...


@runtime_checkable
class MemoryStrategy(Protocol):
    def snapshot(self) -> MemorySnapshot: ...

    def stage(self, delta: MemoryDelta) -> StagedMemoryRevision: ...

    def commit(self, staged: StagedMemoryRevision) -> MemorySnapshot: ...

    def inspect(self, revision: str) -> InspectionModel: ...


@runtime_checkable
class DecisionStrategy(Protocol):
    async def decide(self, bundle: ContextBundle) -> DecisionResult: ...


@runtime_checkable
class ReconsiderationStrategy(Protocol):
    async def evaluate(self, request: ReconsiderationRequest) -> ReconsiderationResult: ...


@runtime_checkable
class ResponseTimingStrategy(Protocol):
    def schedule(self, request: TimingRequest) -> TimingDecision: ...


@runtime_checkable
class ModelAdapter(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...
