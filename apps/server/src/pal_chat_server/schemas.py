from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorBody
    request_id: str


class HealthResponse(BaseModel):
    status: str
    schema_revision: str | None = None


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    path: str


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)


class ModuleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    module_id: str
    config: dict[str, Any] = Field(default_factory=dict)


class ModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    credential_ref: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    display_name: str
    persona_prompt: str
    attention_prior: str
    response_threshold: float = 0.55
    model: ModelBinding


class RuntimeGuardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_llm_calls: int = 32
    max_total_tokens: int = 64_000
    max_auto_retries: int = 2
    worker_restart_limit: int = 1
    pause: bool = False
    stop: bool = False
    log_level: str = "info"


class ExperimentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    template_id: str
    title: str
    modules: dict[str, ModuleSelection]
    agent_a: AgentProfile
    agent_b: AgentProfile
    prompt_version: str = "phase1"
    metadata: dict[str, Any] = Field(default_factory=dict)


class CredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    label: str
    secret: str = Field(min_length=1)


class CredentialRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_ref: str
    provider: str
    label: str
    masked_value: str
    status: str
    created_at: datetime
    updated_at: datetime
    validated_at: datetime | None = None


class CredentialListResponse(BaseModel):
    items: list[CredentialRead]


class CredentialValidateResponse(BaseModel):
    credential: CredentialRead
    ok: bool


class ProfileTemplateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    title: str
    description: str = ""
    profile: ExperimentProfile


class ProfileTemplateUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str = ""
    profile: ExperimentProfile


class ProfileTemplateRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    slug: str
    title: str
    description: str
    profile: ExperimentProfile
    created_at: datetime
    updated_at: datetime


class ProfileTemplateListResponse(BaseModel):
    items: list[ProfileTemplateRead]


class ProfileCloneResponse(BaseModel):
    profile: ExperimentProfile


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=255)
    profile_template_id: str | None = None
    draft_profile: ExperimentProfile | None = None


class DraftProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft_profile: ExperimentProfile


class CatalogMetadataPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, Any]


class GuardrailPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_llm_calls: int | None = None
    max_total_tokens: int | None = None
    max_auto_retries: int | None = None
    worker_restart_limit: int | None = None
    pause: bool | None = None
    stop: bool | None = None
    log_level: str | None = None


class ConversationRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    status: str
    draft_profile: ExperimentProfile
    locked_profile: ExperimentProfile | None
    profile_hash: str | None
    guardrails: RuntimeGuardrails
    catalog_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    validated_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    archive_dir: str | None
    manifest_path: str | None


class ConversationListResponse(BaseModel):
    items: list[ConversationRead]


class ConversationDetailResponse(BaseModel):
    conversation: ConversationRead
    validation: ValidationResult
    manifest: dict[str, Any] | None


class TransitionRule(BaseModel):
    from_status: str
    to_status: str
    via: str


class BootstrapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_name: str
    app_version: str
    generated_at: datetime
    bind_host: str
    module_registry: list[dict[str, Any]]
    templates: list[ProfileTemplateRead]
    credentials: list[CredentialRead]
    state_transitions: list[TransitionRule]
    frontend: dict[str, str]
    note: str | None = None


class MessageSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_message_id: str = Field(min_length=1, max_length=128)
    content_markdown: str = Field(min_length=1)
    mentions: list[str] = Field(default_factory=list)
    primary_reply_to: str | None = None
    responds_to: list[str] = Field(default_factory=list)


class MessageRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    conversation_seq: int
    sender_kind: str
    sender_id: str
    content_markdown: str
    mentions: list[str] = Field(default_factory=list)
    primary_reply_to: str | None
    responds_to: list[str] = Field(default_factory=list)
    client_message_id: str | None
    committed_at: datetime
    cp_revision: int


class MessageCommitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: MessageRead
    cp_revision: CpRevisionRead


class MessageListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MessageRead]


class SegmentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segment_id: str
    ordinal: int
    status: str
    message_refs: list[str] = Field(default_factory=list)
    start_seq: int | None
    end_seq: int | None
    title: str
    summary: str
    base_activation: float
    activation_updated_at: datetime
    created_revision: int
    closed_revision: int | None
    projection_trace_ref: str


class CpSnapshotRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[SegmentRead] = Field(default_factory=list)
    last_operation: str | None = None


class CpRevisionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projection_revision: int
    covered_through_seq: int
    strategy_id: str
    strategy_version: str
    snapshot: CpSnapshotRead
    trace_ref: str
    created_at: datetime


class CpRevisionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CpRevisionRead]


class OutboxEventEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = 1
    event_id: str
    event_type: str
    conversation_id: str
    emitted_at: datetime | None = None
    conversation_seq: int | None = None
    payload: dict[str, Any]


class SessionSnapshotEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: int = 1
    event_type: str = "session.snapshot"
    conversation_id: str
    payload: dict[str, Any]
