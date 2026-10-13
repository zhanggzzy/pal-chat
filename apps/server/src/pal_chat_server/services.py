from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pal_chat_server.errors import AppError
from pal_chat_server.models import (
    AgentTopicIndex,
    ChatRun,
    ContextSnapshot,
    Conversation,
    Message,
    MessageTopicLink,
    ModelCall,
    Participant,
    ParticipantKind,
    RequestIdempotency,
    ResponseDecision,
    RunEvent,
    RunStatus,
    Topic,
    TopicSummaryRevision,
    TopicTransition,
)


@dataclass(slots=True)
class TopicDetail:
    topic: Topic
    transitions: list[TopicTransition]
    summaries: list[TopicSummaryRevision]


@dataclass(slots=True)
class MessageAnalysis:
    message: Message
    topic_links: list[MessageTopicLink]
    agent_indexes: list[AgentTopicIndex]
    snapshots: list[ContextSnapshot]
    decisions: list[ResponseDecision]
    model_calls: list[ModelCall]


def utc_now() -> datetime:
    return datetime.now(UTC)


DEFAULT_PARTICIPANTS: tuple[tuple[str, str | None, float | None], ...] = (
    ("User", None, None),
    (
        "Atlas",
        "A pragmatic collaborator who adds structured next steps and concise synthesis.",
        0.62,
    ),
    (
        "Mira",
        "A curious counterpart who challenges assumptions and surfaces alternative angles.",
        0.62,
    ),
)


def hash_request(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def next_sequence(session: Session, conversation_id: str) -> int:
    max_seq = session.scalar(
        select(func.max(Message.sequence_no)).where(Message.conversation_id == conversation_id)
    )
    return int(max_seq or 0) + 1


def next_event_seq(session: Session, conversation_id: str) -> int:
    max_seq = session.scalar(
        select(func.max(RunEvent.seq)).where(RunEvent.conversation_id == conversation_id)
    )
    return int(max_seq or 0) + 1


def add_event(
    session: Session,
    *,
    conversation_id: str,
    run_id: str | None,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, object],
) -> RunEvent:
    event = RunEvent(
        id=str(uuid4()),
        conversation_id=conversation_id,
        run_id=run_id,
        seq=next_event_seq(session, conversation_id),
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        public_payload_json=payload,
        created_at=utc_now(),
    )
    session.add(event)
    return event


def get_conversation_or_404(session: Session, conversation_id: str) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None:
        raise AppError(
            code="conversation_not_found",
            status_code=404,
            message="Conversation not found.",
        )
    return conversation


def get_run_or_404(session: Session, run_id: str) -> ChatRun:
    run = session.get(ChatRun, run_id)
    if run is None:
        raise AppError(code="run_not_found", status_code=404, message="Run not found.")
    return run


def get_message_or_404(session: Session, message_id: str) -> Message:
    message = session.get(Message, message_id)
    if message is None:
        raise AppError(code="message_not_found", status_code=404, message="Message not found.")
    return message


def get_topic_or_404(session: Session, topic_id: str) -> Topic:
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise AppError(code="topic_not_found", status_code=404, message="Topic not found.")
    return topic


def get_active_run(session: Session, conversation_id: str) -> ChatRun | None:
    return session.scalar(
        select(ChatRun)
        .where(
            ChatRun.conversation_id == conversation_id,
            ChatRun.status.in_(
                [RunStatus.QUEUED.value, RunStatus.RUNNING.value, RunStatus.STOP_REQUESTED.value]
            ),
        )
        .order_by(ChatRun.started_at.desc().nullslast(), ChatRun.id.desc())
    )


def raise_integrity_conflict(exc: IntegrityError) -> None:
    message = str(exc.orig)
    if "request_idempotency" in message:
        raise AppError(
            code="idempotency_conflict",
            status_code=409,
            message="Idempotency key already used with different payload.",
        ) from exc
    if "chat_runs.conversation_id" in message:
        raise AppError(
            code="run_in_progress",
            status_code=409,
            message="A relay run is already active.",
        ) from exc
    raise exc


def create_conversation(session: Session, *, title: str) -> tuple[Conversation, list[Participant]]:
    now = utc_now()
    conversation = Conversation(
        id=str(uuid4()),
        title=title,
        active_topic_id=None,
        created_at=now,
        updated_at=now,
    )
    session.add(conversation)
    participants: list[Participant] = []
    for index, (display_name, role_prompt, threshold) in enumerate(DEFAULT_PARTICIPANTS):
        participant = Participant(
            id=str(uuid4()),
            conversation_id=conversation.id,
            kind=ParticipantKind.USER.value if index == 0 else ParticipantKind.AGENT.value,
            display_name=display_name,
            role_prompt=role_prompt,
            decision_threshold=threshold,
            sort_order=index,
        )
        participants.append(participant)
        session.add(participant)
    session.flush()
    return conversation, participants


def delete_conversation(session: Session, *, conversation_id: str) -> None:
    conversation = get_conversation_or_404(session, conversation_id)
    session.delete(conversation)


def create_message_with_run(
    session: Session,
    *,
    conversation_id: str,
    content: str,
    idempotency_key: str,
) -> dict[str, str]:
    conversation = get_conversation_or_404(session, conversation_id)
    request_hash = hash_request(content)
    existing = session.get(RequestIdempotency, (conversation_id, idempotency_key))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise AppError(
                code="idempotency_conflict",
                status_code=409,
                message="Idempotency key already used with different payload.",
            )
        response = existing.response_body_json
        return {
            "message_id": str(response["message_id"]),
            "run_id": str(response["run_id"]),
            "status": str(response["status"]),
        }

    if get_active_run(session, conversation_id) is not None:
        raise AppError(
            code="run_in_progress",
            status_code=409,
            message="A relay run is already active.",
        )

    user = session.scalar(
        select(Participant)
        .where(
            Participant.conversation_id == conversation_id,
            Participant.kind == ParticipantKind.USER.value,
        )
        .limit(1)
    )
    if user is None:
        raise AppError(
            code="conversation_corrupt",
            status_code=500,
            message="Conversation is missing a user participant.",
        )

    now = utc_now()
    try:
        with session.begin_nested():
            run = ChatRun(
                id=str(uuid4()),
                conversation_id=conversation.id,
                root_user_message_id=None,
                status=RunStatus.QUEUED.value,
                stop_reason=None,
                max_agent_messages=5,
                max_rounds=6,
                max_total_tokens=30_000,
                timeout_ms=90_000,
                agent_message_count=0,
                round_count=0,
                reserved_tokens=0,
                actual_tokens=0,
                cancel_generation=0,
                started_at=now,
                ended_at=None,
            )
            session.add(run)
            session.flush()

            message = Message(
                id=str(uuid4()),
                conversation_id=conversation.id,
                run_id=run.id,
                author_participant_id=user.id,
                parent_message_id=None,
                caused_by_decision_id=None,
                kind=ParticipantKind.USER.value,
                content=content,
                sequence_no=next_sequence(session, conversation_id),
                created_at=now,
            )
            session.add(message)
            session.flush()
            run.root_user_message_id = message.id
            add_event(
                session,
                conversation_id=conversation.id,
                run_id=run.id,
                event_type="message.created",
                entity_type="message",
                entity_id=message.id,
                payload={"sequence_no": message.sequence_no, "kind": message.kind},
            )

            response = {"message_id": message.id, "run_id": run.id, "status": run.status}
            session.add(
                RequestIdempotency(
                    conversation_id=conversation.id,
                    key=idempotency_key,
                    request_hash=request_hash,
                    response_status=202,
                    response_body_json=response,
                    created_at=now,
                )
            )
            session.flush()
    except IntegrityError as exc:
        existing = session.get(RequestIdempotency, (conversation_id, idempotency_key))
        if existing is not None:
            if existing.request_hash != request_hash:
                raise AppError(
                    code="idempotency_conflict",
                    status_code=409,
                    message="Idempotency key already used with different payload.",
                ) from exc
            response = existing.response_body_json
            return {
                "message_id": str(response["message_id"]),
                "run_id": str(response["run_id"]),
                "status": str(response["status"]),
            }
        raise_integrity_conflict(exc)

    conversation.updated_at = now
    return response


def stop_run(
    session: Session,
    *,
    run_id: str,
    idempotency_key: str,
) -> dict[str, str]:
    run = get_run_or_404(session, run_id)
    request_hash = hash_request(f"stop:{run.id}")
    existing = session.get(RequestIdempotency, (run.conversation_id, idempotency_key))
    if existing is not None:
        if existing.request_hash != request_hash:
            raise AppError(
                code="idempotency_conflict",
                status_code=409,
                message="Idempotency key already used with different payload.",
            )
        response = existing.response_body_json
        return {"run_id": str(response["run_id"]), "status": str(response["status"])}

    if run.status in {RunStatus.STOPPED.value, RunStatus.FAILED.value, RunStatus.COMPLETED.value}:
        status = run.status
    else:
        run.status = RunStatus.STOP_REQUESTED.value
        run.stop_reason = "user_stop"
        run.cancel_generation += 1
        add_event(
            session,
            conversation_id=run.conversation_id,
            run_id=run.id,
            event_type="run.stop_requested",
            entity_type="run",
            entity_id=run.id,
            payload={"status": run.status},
        )
        status = run.status

    response = {"run_id": run.id, "status": status}
    session.add(
        RequestIdempotency(
            conversation_id=run.conversation_id,
            key=idempotency_key,
            request_hash=request_hash,
            response_status=202,
            response_body_json=response,
            created_at=utc_now(),
        )
    )
    return response


def list_messages(
    session: Session,
    *,
    conversation_id: str,
    after_sequence: int | None,
    limit: int,
) -> list[Message]:
    get_conversation_or_404(session, conversation_id)
    stmt: Select[tuple[Message]] = select(Message).where(Message.conversation_id == conversation_id)
    if after_sequence is not None:
        stmt = stmt.where(Message.sequence_no > after_sequence)
    return list(session.scalars(stmt.order_by(Message.sequence_no.asc()).limit(limit)).all())


def list_topics(session: Session, *, conversation_id: str) -> list[Topic]:
    get_conversation_or_404(session, conversation_id)
    return list(
        session.scalars(
            select(Topic)
            .where(Topic.conversation_id == conversation_id)
            .order_by(Topic.updated_at.desc())
        )
    )


def get_topic_detail(session: Session, *, topic_id: str) -> TopicDetail:
    topic = get_topic_or_404(session, topic_id)
    transitions = list(
        session.execute(
            select(TopicTransition)
            .where(TopicTransition.to_topic_id == topic.id)
            .order_by(TopicTransition.created_at)
        ).scalars()
    )
    summaries = list(
        session.execute(
            select(TopicSummaryRevision)
            .where(TopicSummaryRevision.topic_id == topic.id)
            .order_by(TopicSummaryRevision.created_at)
        ).scalars()
    )
    return TopicDetail(topic=topic, transitions=transitions, summaries=summaries)


def get_message_analysis(session: Session, *, message_id: str) -> MessageAnalysis:
    message = get_message_or_404(session, message_id)
    topic_links = [
        item
        for item in session.scalars(
            select(MessageTopicLink).where(MessageTopicLink.message_id == message.id)
        ).all()
    ]
    snapshots = [
        item
        for item in session.scalars(
            select(ContextSnapshot).where(ContextSnapshot.trigger_message_id == message.id)
        ).all()
    ]
    decisions = [
        item
        for item in session.scalars(
            select(ResponseDecision).where(ResponseDecision.trigger_message_id == message.id)
        ).all()
    ]
    run_id = message.run_id or ""
    model_calls = [
        item
        for item in session.scalars(select(ModelCall).where(ModelCall.run_id == run_id)).all()
    ]
    agent_indexes = [
        item
        for item in session.scalars(
            select(AgentTopicIndex)
            .where(AgentTopicIndex.last_seen_message_id == message.id)
            .order_by(AgentTopicIndex.updated_at.desc())
        ).all()
    ]
    return MessageAnalysis(
        message=message,
        topic_links=topic_links,
        agent_indexes=agent_indexes,
        snapshots=snapshots,
        decisions=decisions,
        model_calls=model_calls,
    )


def list_timeline(session: Session, *, run_id: str, after_seq: int | None) -> list[RunEvent]:
    run = get_run_or_404(session, run_id)
    stmt: Select[tuple[RunEvent]] = select(RunEvent).where(RunEvent.run_id == run.id)
    if after_seq is not None:
        stmt = stmt.where(RunEvent.seq > after_seq)
    return list(session.scalars(stmt.order_by(RunEvent.seq.asc())).all())


def list_events(session: Session, *, conversation_id: str, after_seq: int | None) -> list[RunEvent]:
    get_conversation_or_404(session, conversation_id)
    stmt: Select[tuple[RunEvent]] = select(RunEvent).where(
        RunEvent.conversation_id == conversation_id
    )
    if after_seq is not None:
        stmt = stmt.where(RunEvent.seq > after_seq)
    return list(session.scalars(stmt.order_by(RunEvent.seq.asc())).all())


def ensure_primary_topic_link(
    session: Session,
    *,
    message_id: str,
    topic_id: str,
    confidence: float = 1.0,
    route_action: str = "manual",
    route_method: str = "rule",
    signals: list[str] | None = None,
    policy_version: str = "v1",
) -> MessageTopicLink:
    message = get_message_or_404(session, message_id)
    topic = get_topic_or_404(session, topic_id)
    if message.conversation_id != topic.conversation_id:
        raise AppError(
            code="cross_conversation_topic_link",
            status_code=400,
            message="Message and topic belong to different conversations.",
        )
    existing = session.scalar(
        select(MessageTopicLink).where(
            MessageTopicLink.message_id == message_id, MessageTopicLink.kind == "primary"
        )
    )
    if existing is not None:
        raise AppError(
            code="primary_topic_exists",
            status_code=409,
            message="Message already has a primary topic link.",
        )
    link = MessageTopicLink(
        id=str(uuid4()),
        message_id=message_id,
        topic_id=topic_id,
        kind="primary",
        confidence=confidence,
        route_action=route_action,
        route_method=route_method,
        signals_json=signals or [],
        policy_version=policy_version,
        is_override=False,
        created_at=utc_now(),
    )
    session.add(link)
    return link
