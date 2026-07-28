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
    topic_links: list[dict[str, Any]]
    agent_indexes: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    model_calls: list[dict[str, Any]]


class TopicDetailResponse(BaseModel):
    topic: TopicRead
    transitions: list[dict[str, Any]]
    summaries: list[dict[str, Any]]


class HealthResponse(BaseModel):
    status: str
    schema_revision: str | None = None

