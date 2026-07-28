from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: dict[str, Any]


class ParticipantRead(BaseModel):
    id: str
    conversation_id: str
    kind: str
    display_name: str
    role_prompt: str | None
    decision_threshold: float | None
    sort_order: int


class ConversationRead(BaseModel):
    id: str
    title: str
    active_topic_id: str | None
    created_at: datetime
    updated_at: datetime


class RunRead(BaseModel):
    id: str
    conversation_id: str
    root_user_message_id: str | None
    status: str
    stop_reason: str | None
    max_agent_messages: int
    max_rounds: int
    max_total_tokens: int
    timeout_ms: int
    agent_message_count: int
    round_count: int
    reserved_tokens: int
    actual_tokens: int
    cancel_generation: int
    started_at: datetime | None
    ended_at: datetime | None


class MessageRead(BaseModel):
    id: str
    conversation_id: str
    run_id: str | None
    author_participant_id: str
    parent_message_id: str | None
    caused_by_decision_id: str | None
    kind: str
    content: str
    sequence_no: int
    created_at: datetime


class TopicRead(BaseModel):
    id: str
    conversation_id: str
    title: str
    status: str
    interrupted_topic_id: str | None
    current_summary_revision_id: str | None
    created_by_message_id: str
    created_at: datetime
    updated_at: datetime


class TopicTransitionRead(BaseModel):
    id: str
    conversation_id: str
    from_topic_id: str | None
    to_topic_id: str
    cause_message_id: str
    action: str
    previous_status_json: dict[str, Any]
    created_at: datetime


class TopicSummaryRevisionRead(BaseModel):
    id: str
    topic_id: str
    content: str
    through_message_id: str
    model_call_id: str | None
    estimated_tokens: int
    created_at: datetime


class MessageTopicLinkRead(BaseModel):
    id: str
    message_id: str
    topic_id: str
    kind: str
    confidence: float
    route_action: str
    route_method: str
    signals_json: list[str]
    policy_version: str
    is_override: bool
    created_at: datetime


class AgentTopicIndexRead(BaseModel):
    agent_id: str
    topic_id: str
    salience: float
    role_affinity: float
    familiarity: float
    last_seen_message_id: str | None
    last_selected_at: datetime | None
    reason_codes_json: list[str]
    index_version: str
    updated_at: datetime


class ContextSnapshotRead(BaseModel):
    id: str
    run_id: str
    trigger_message_id: str
    agent_id: str
    purpose: str
    policy_version: str
    tokenizer: str
    estimated_tokens: int
    canonical_content_hash: str
    created_at: datetime


class ResponseDecisionRead(BaseModel):
    id: str
    run_id: str
    trigger_message_id: str
    agent_id: str
    snapshot_id: str
    model_call_id: str | None
    eligible: bool
    should_reply: bool
    score: float
    threshold: float
    dimensions_json: dict[str, Any]
    reason_codes_json: list[str]
    reply_intent: str
    outcome: str
    policy_version: str
    created_at: datetime


class ModelCallRead(BaseModel):
    id: str
    run_id: str
    purpose: str
    agent_id: str | None
    provider: str
    model: str
    provider_request_id: str | None
    status: str
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    token_count_source: str
    latency_ms: int
    pricing_version: str | None
    estimated_cost_micros: int | None
    error_category: str | None
    started_at: datetime
    ended_at: datetime


class SSEEventRead(BaseModel):
    event_id: str
    seq: int
    type: str
    conversation_id: str
    run_id: str | None
    entity_id: str
    occurred_at: datetime
    schema_version: int = 1
    data: dict[str, Any]


class ConversationCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class ConversationCreateResponse(BaseModel):
    conversation: ConversationRead
    participants: list[ParticipantRead]


class MessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=8_000)


class MessageCreateResponse(BaseModel):
    message_id: str
    run_id: str
    status: str


class RunStopResponse(BaseModel):
    run_id: str
    status: str


class ConversationDetailResponse(BaseModel):
    conversation: ConversationRead
    participants: list[ParticipantRead]
    active_run: RunRead | None


class MessageListResponse(BaseModel):
    items: list[MessageRead]


class TopicListResponse(BaseModel):
    items: list[TopicRead]


class TimelineResponse(BaseModel):
    items: list[SSEEventRead]


class MessageAnalysisResponse(BaseModel):
    message: MessageRead
    topic_links: list[MessageTopicLinkRead]
    agent_indexes: list[AgentTopicIndexRead]
    snapshots: list[ContextSnapshotRead]
    decisions: list[ResponseDecisionRead]
    model_calls: list[ModelCallRead]


class TopicDetailResponse(BaseModel):
    topic: TopicRead
    transitions: list[TopicTransitionRead]
    summaries: list[TopicSummaryRevisionRead]


class HealthResponse(BaseModel):
    status: str
    schema_revision: str | None = None
