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
    causal_episode_id: str | None = None
    caused_by_message_id: str | None = None
    agent_hop: int = 0
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


class RunAttemptRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    run_id: str
    agent_id: str
    phase: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    latency_ms: int | None
    bundle_revision: str | None = None
    memory_revision_before: str | None = None
    memory_revision_after: str | None = None
    staged_memory_revision: str | None = None
    payload: dict[str, Any] | None = None
    created_at: datetime


class AttemptListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RunAttemptRead]


class AgentRunRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    agent_id: str
    status: str
    phase: str
    observation_message_ids: list[str] = Field(default_factory=list)
    root_message_ids: list[str] = Field(default_factory=list)
    expected_conversation_seq: int
    profile_hash: str
    idempotency_key: str
    causal_episode_id: str | None = None
    caused_by_message_id: str | None = None
    agent_hop: int = 0
    decision_json: dict[str, Any] | None = None
    draft_message_json: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    latency_ms: int | None = None
    attempts: list[RunAttemptRead] = Field(default_factory=list)


class AgentRunListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AgentRunRead]


class MemoryRevisionRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str
    counter: int = 0
    module_id: str
    payload: dict[str, Any]
    committed_at: datetime | None = None
    run_id: str | None = None
    conversation_seq: int | None = None
    cp_revision: int | None = None
    bundle_revisions: list[str] = Field(default_factory=list)


class MemoryRevisionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MemoryRevisionRead]


class ContextBundleRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_id: str
    revision: str
    phase: str
    conversation_seq: int
    projection_revision: int
    memory_revision: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    selected_public_refs: list[str] = Field(default_factory=list)
    selected_private_refs: list[str] = Field(default_factory=list)
    selection_trace: list[str] = Field(default_factory=list)
    observation_message_ids: list[str] = Field(default_factory=list)
    estimated_tokens: int
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    rendered_items: list[str] = Field(default_factory=list)
    as_of_time: datetime


class ContextBundleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ContextBundleRead]


class LogEntryRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    timestamp: datetime
    level: str
    phase: str | None = None
    message: str
    agent_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    details: dict[str, Any] | None = None


class LogEntryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[LogEntryRead]


class CausalEpisodeRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    episode_id: str
    root_message_ids: list[str] = Field(default_factory=list)
    total_actions: int
    agent_a_actions: int
    agent_b_actions: int
    max_agent_hop: int
    updated_at: datetime


class CausalEpisodeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CausalEpisodeRead]


class CostBucketRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: int
    cost_usd: float


class CostBreakdownRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tokens: int
    total_cost_usd: float
    by_agent: dict[str, CostBucketRead] = Field(default_factory=dict)
    by_phase: dict[str, CostBucketRead] = Field(default_factory=dict)


class AutomaticMetricsRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    conversation_id: str
    computed_at: datetime
    metrics: dict[str, Any] = Field(default_factory=dict)


class ManualScoreItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: str
    score: float | None = None
    note: str = ""


class ManualScoreRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    conversation_id: str
    rubric_version: str
    updated_at: datetime | None = None
    scores: list[ManualScoreItem] = Field(default_factory=list)
    overall_score: float | None = None
    overall_note: str = ""


class ManualScoreUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rubric_version: str = Field(min_length=1, max_length=128)
    scores: list[ManualScoreItem] = Field(default_factory=list)
    overall_note: str = ""


class HistoryEntryRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str
    title: str | None = None
    status: str | None = None
    created_at: datetime | None = None
    ended_at: datetime | None = None
    archive_dir: str | None = None
    manifest_path: str | None = None
    profile_hash: str | None = None
    adapter_module_id: str | None = None
    adapter_known: bool = False


class HistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[HistoryEntryRead] = Field(default_factory=list)


class HistoryDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry: HistoryEntryRead
    raw_manifest: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    cp_revisions: list[dict[str, Any]] = Field(default_factory=list)
    logs: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisExportJobRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    conversation_id: str
    status: str
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    download_url: str | None = None
