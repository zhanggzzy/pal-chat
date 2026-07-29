from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class ParticipantKind(StrEnum):
    USER = "user"
    AGENT = "agent"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    STOP_REQUESTED = "stop_requested"
    STOPPED = "stopped"
    FAILED = "failed"
    COMPLETED = "completed"


class TopicStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    active_topic_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    display_name: Mapped[str] = mapped_column(String(128))
    role_prompt: Mapped[str | None] = mapped_column(Text)
    decision_threshold: Mapped[float | None] = mapped_column(Float)
    sort_order: Mapped[int] = mapped_column(Integer)


class ChatRun(Base):
    __tablename__ = "chat_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    root_user_message_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(32))
    stop_reason: Mapped[str | None] = mapped_column(String(64))
    max_agent_messages: Mapped[int] = mapped_column(Integer)
    max_rounds: Mapped[int] = mapped_column(Integer)
    max_total_tokens: Mapped[int] = mapped_column(Integer)
    timeout_ms: Mapped[int] = mapped_column(Integer)
    agent_message_count: Mapped[int] = mapped_column(Integer, default=0)
    round_count: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    actual_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cancel_generation: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence_no", name="uq_messages_sequence"),
        UniqueConstraint("caused_by_decision_id", name="uq_messages_caused_by_decision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("chat_runs.id"))
    author_participant_id: Mapped[str] = mapped_column(ForeignKey("participants.id"))
    parent_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"))
    caused_by_decision_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    sequence_no: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))
    interrupted_topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"))
    current_summary_revision_id: Mapped[str | None] = mapped_column(String(36))
    created_by_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MessageTopicLink(Base):
    __tablename__ = "message_topic_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    route_action: Mapped[str] = mapped_column(String(32))
    route_method: Mapped[str] = mapped_column(String(32))
    signals_json: Mapped[list[str]] = mapped_column(JSON)
    policy_version: Mapped[str] = mapped_column(String(32))
    is_override: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TopicTransition(Base):
    __tablename__ = "topic_transitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    from_topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"))
    to_topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id"))
    cause_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    action: Mapped[str] = mapped_column(String(32))
    previous_status_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("chat_runs.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(32))
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("participants.id"))
    provider: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    token_count_source: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    pricing_version: Mapped[str | None] = mapped_column(String(32))
    estimated_cost_micros: Mapped[int | None] = mapped_column(Integer)
    error_category: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class TopicSummaryRevision(Base):
    __tablename__ = "topic_summary_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    through_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    model_call_id: Mapped[str | None] = mapped_column(ForeignKey("model_calls.id"))
    estimated_tokens: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentTopicIndex(Base):
    __tablename__ = "agent_topic_indexes"

    agent_id: Mapped[str] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    topic_id: Mapped[str] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"),
        primary_key=True,
    )
    salience: Mapped[float] = mapped_column(Float)
    role_affinity: Mapped[float] = mapped_column(Float)
    familiarity: Mapped[float] = mapped_column(Float)
    last_seen_message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"))
    last_selected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON)
    index_version: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContextSnapshot(Base):
    __tablename__ = "context_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("chat_runs.id", ondelete="CASCADE"))
    trigger_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("participants.id"))
    purpose: Mapped[str] = mapped_column(String(16))
    policy_version: Mapped[str] = mapped_column(String(32))
    tokenizer: Mapped[str] = mapped_column(String(64))
    estimated_tokens: Mapped[int] = mapped_column(Integer)
    canonical_content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ContextSnapshotItem(Base):
    __tablename__ = "context_snapshot_items"
    __table_args__ = (UniqueConstraint("snapshot_id", "ordinal", name="uq_snapshot_item_ordinal"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("context_snapshots.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    item_type: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"))
    summary_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("topic_summary_revisions.id")
    )
    rendered_content: Mapped[str] = mapped_column(Text)
    estimated_tokens: Mapped[int] = mapped_column(Integer)
    inclusion_reason: Mapped[str] = mapped_column(String(64))


class ResponseDecision(Base):
    __tablename__ = "response_decisions"
    __table_args__ = (
        UniqueConstraint("run_id", "trigger_message_id", "agent_id", name="uq_decision_once"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("chat_runs.id", ondelete="CASCADE"))
    trigger_message_id: Mapped[str] = mapped_column(ForeignKey("messages.id"))
    agent_id: Mapped[str] = mapped_column(ForeignKey("participants.id"))
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("context_snapshots.id", ondelete="CASCADE"))
    model_call_id: Mapped[str | None] = mapped_column(ForeignKey("model_calls.id"))
    eligible: Mapped[bool] = mapped_column(Boolean)
    should_reply: Mapped[bool] = mapped_column(Boolean)
    score: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    dimensions_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON)
    reply_intent: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_run_event_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("chat_runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(36))
    public_payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RequestIdempotency(Base):
    __tablename__ = "request_idempotency"

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
