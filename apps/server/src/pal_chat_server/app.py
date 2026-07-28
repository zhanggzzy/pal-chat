from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pal_chat_server.config import get_settings
from pal_chat_server.db import get_db_session, get_engine
from pal_chat_server.errors import AppError, build_error_response
from pal_chat_server.models import ChatRun, Conversation, Participant, RunEvent, Topic
from pal_chat_server.schemas import (
    AgentTopicIndexRead,
    ContextSnapshotRead,
    ConversationCreateRequest,
    ConversationCreateResponse,
    ConversationDetailResponse,
    ConversationRead,
    ErrorEnvelope,
    HealthResponse,
    MessageAnalysisResponse,
    MessageCreateRequest,
    MessageCreateResponse,
    MessageListResponse,
    MessageRead,
    MessageTopicLinkRead,
    ModelCallRead,
    ParticipantRead,
    ResponseDecisionRead,
    RunRead,
    RunStopResponse,
    SSEEventRead,
    TimelineResponse,
    TopicDetailResponse,
    TopicListResponse,
    TopicRead,
    TopicSummaryRevisionRead,
    TopicTransitionRead,
)
from pal_chat_server.services import (
    create_conversation,
    create_message_with_run,
    delete_conversation,
    get_active_run,
    get_conversation_or_404,
    get_message_analysis,
    get_run_or_404,
    get_topic_detail,
    list_events,
    list_messages,
    list_timeline,
    list_topics,
    stop_run,
)


def to_conversation_read(item: Conversation) -> ConversationRead:
    return ConversationRead.model_validate(item, from_attributes=True)


def to_participant_read(item: Participant) -> ParticipantRead:
    return ParticipantRead.model_validate(item, from_attributes=True)


def to_run_read(item: ChatRun) -> RunRead:
    return RunRead.model_validate(item, from_attributes=True)


def to_message_read(item: object) -> MessageRead:
    return MessageRead.model_validate(item, from_attributes=True)


def to_topic_read(item: Topic) -> TopicRead:
    return TopicRead.model_validate(item, from_attributes=True)


def to_topic_transition_read(item: object) -> TopicTransitionRead:
    return TopicTransitionRead.model_validate(item, from_attributes=True)


def to_topic_summary_revision_read(item: object) -> TopicSummaryRevisionRead:
    return TopicSummaryRevisionRead.model_validate(item, from_attributes=True)


def to_message_topic_link_read(item: object) -> MessageTopicLinkRead:
    return MessageTopicLinkRead.model_validate(item, from_attributes=True)


def to_agent_topic_index_read(item: object) -> AgentTopicIndexRead:
    return AgentTopicIndexRead.model_validate(item, from_attributes=True)


def to_context_snapshot_read(item: object) -> ContextSnapshotRead:
    return ContextSnapshotRead.model_validate(item, from_attributes=True)


def to_response_decision_read(item: object) -> ResponseDecisionRead:
    return ResponseDecisionRead.model_validate(item, from_attributes=True)


def to_model_call_read(item: object) -> ModelCallRead:
    return ModelCallRead.model_validate(item, from_attributes=True)


def to_event_read(item: RunEvent) -> SSEEventRead:
    return SSEEventRead(
        event_id=item.id,
        seq=item.seq,
        type=item.event_type,
        conversation_id=item.conversation_id,
        run_id=item.run_id,
        entity_id=item.entity_id,
        occurred_at=item.created_at,
        data=item.public_payload_json,
    )


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_engine(get_settings())
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="pal-chat API",
        version="0.1.0",
        lifespan=lifespan,
        responses={
            400: {"model": ErrorEnvelope},
            404: {"model": ErrorEnvelope},
            409: {"model": ErrorEnvelope},
        },
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def add_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid4()))
        return build_error_response(
            request_id=request_id,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid4()))
        return build_error_response(
            request_id=request_id,
            status_code=422,
            code="validation_error",
            message="Request validation failed.",
            details={"issues": exc.errors()},
        )

    @app.get("/health/live", response_model=HealthResponse, tags=["health"])
    def live_health() -> HealthResponse:
        return HealthResponse(status="live")

    @app.get("/health/ready", response_model=HealthResponse, tags=["health"])
    def ready_health(db: Session = Depends(get_db_session)) -> HealthResponse:
        engine = db.get_bind()
        assert isinstance(engine, Engine)
        try:
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                connection.exec_driver_sql("ROLLBACK")
        except OperationalError as exc:
            raise AppError(
                code="schema_not_ready",
                status_code=503,
                message="Database schema is not ready.",
                details={"expected": settings.ready_schema_revision, "actual": None},
            ) from exc
        if revision != settings.ready_schema_revision:
            raise AppError(
                code="schema_not_ready",
                status_code=503,
                message="Database schema is not at the expected revision.",
                details={"expected": settings.ready_schema_revision, "actual": revision},
            )
        return HealthResponse(status="ready", schema_revision=revision)

    @app.post(
        "/api/v1/conversations",
        status_code=status.HTTP_201_CREATED,
        response_model=ConversationCreateResponse,
        tags=["conversations"],
    )
    def post_conversation(
        payload: ConversationCreateRequest,
        db: Session = Depends(get_db_session),
    ) -> ConversationCreateResponse:
        conversation, participants = create_conversation(db, title=payload.title)
        db.commit()
        db.refresh(conversation)
        return ConversationCreateResponse(
            conversation=to_conversation_read(conversation),
            participants=[to_participant_read(item) for item in participants],
        )

    @app.delete(
        "/api/v1/conversations/{conversation_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["conversations"],
    )
    def remove_conversation(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> Response:
        delete_conversation(db, conversation_id=conversation_id)
        db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(
        "/api/v1/conversations/{conversation_id}",
        response_model=ConversationDetailResponse,
        tags=["conversations"],
    )
    def get_conversation_detail(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ConversationDetailResponse:
        conversation = get_conversation_or_404(db, conversation_id)
        participants = list(
            db.query(Participant)
            .filter(Participant.conversation_id == conversation_id)
            .order_by(Participant.sort_order)
        )
        active_run = get_active_run(db, conversation_id)
        return ConversationDetailResponse(
            conversation=to_conversation_read(conversation),
            participants=[to_participant_read(item) for item in participants],
            active_run=to_run_read(active_run) if active_run is not None else None,
        )

    @app.post(
        "/api/v1/conversations/{conversation_id}/messages",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=MessageCreateResponse,
        tags=["messages"],
    )
    def post_message(
        conversation_id: str,
        payload: MessageCreateRequest,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        db: Session = Depends(get_db_session),
    ) -> MessageCreateResponse:
        result = create_message_with_run(
            db,
            conversation_id=conversation_id,
            content=payload.content,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return MessageCreateResponse(**result)

    @app.get(
        "/api/v1/conversations/{conversation_id}/messages",
        response_model=MessageListResponse,
        tags=["messages"],
    )
    def get_messages(
        conversation_id: str,
        after_sequence: int | None = Query(default=None, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        db: Session = Depends(get_db_session),
    ) -> MessageListResponse:
        items = list_messages(
            db,
            conversation_id=conversation_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return MessageListResponse(items=[to_message_read(item) for item in items])

    @app.get(
        "/api/v1/conversations/{conversation_id}/topics",
        response_model=TopicListResponse,
        tags=["topics"],
    )
    def get_topics(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> TopicListResponse:
        items = list_topics(db, conversation_id=conversation_id)
        return TopicListResponse(items=[to_topic_read(item) for item in items])

    @app.get("/api/v1/topics/{topic_id}", response_model=TopicDetailResponse, tags=["topics"])
    def get_topic(topic_id: str, db: Session = Depends(get_db_session)) -> TopicDetailResponse:
        detail = get_topic_detail(db, topic_id=topic_id)
        return TopicDetailResponse(
            topic=to_topic_read(detail.topic),
            transitions=[to_topic_transition_read(item) for item in detail.transitions],
            summaries=[to_topic_summary_revision_read(item) for item in detail.summaries],
        )

    @app.get(
        "/api/v1/messages/{message_id}/analysis",
        response_model=MessageAnalysisResponse,
        tags=["analysis"],
    )
    def get_analysis(
        message_id: str,
        db: Session = Depends(get_db_session),
    ) -> MessageAnalysisResponse:
        analysis = get_message_analysis(db, message_id=message_id)
        return MessageAnalysisResponse(
            message=to_message_read(analysis.message),
            topic_links=[to_message_topic_link_read(item) for item in analysis.topic_links],
            agent_indexes=[to_agent_topic_index_read(item) for item in analysis.agent_indexes],
            snapshots=[to_context_snapshot_read(item) for item in analysis.snapshots],
            decisions=[to_response_decision_read(item) for item in analysis.decisions],
            model_calls=[to_model_call_read(item) for item in analysis.model_calls],
        )

    @app.get("/api/v1/runs/{run_id}", response_model=RunRead, tags=["runs"])
    def get_run(run_id: str, db: Session = Depends(get_db_session)) -> RunRead:
        run = get_run_or_404(db, run_id)
        return to_run_read(run)

    @app.post(
        "/api/v1/runs/{run_id}/stop",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=RunStopResponse,
        tags=["runs"],
    )
    def post_stop_run(
        run_id: str,
        idempotency_key: str = Header(..., alias="Idempotency-Key"),
        db: Session = Depends(get_db_session),
    ) -> RunStopResponse:
        response = stop_run(db, run_id=run_id, idempotency_key=idempotency_key)
        db.commit()
        return RunStopResponse(**response)

    @app.get("/api/v1/runs/{run_id}/timeline", response_model=TimelineResponse, tags=["runs"])
    def get_run_timeline(
        run_id: str,
        after_seq: int | None = Query(default=None, ge=0),
        db: Session = Depends(get_db_session),
    ) -> TimelineResponse:
        events = list_timeline(db, run_id=run_id, after_seq=after_seq)
        return TimelineResponse(items=[to_event_read(item) for item in events])

    @app.get("/api/v1/conversations/{conversation_id}/events", tags=["events"])
    async def stream_events(
        conversation_id: str,
        after_seq: int | None = Query(default=None, ge=0),
        db: Session = Depends(get_db_session),
    ) -> StreamingResponse:
        events = list_events(db, conversation_id=conversation_id, after_seq=after_seq)

        async def event_stream() -> AsyncIterator[str]:
            for item in events:
                payload = to_event_read(item).model_dump(mode="json")
                yield f"id: {item.seq}\nevent: {item.event_type}\ndata: {json.dumps(payload)}\n\n"
            yield ": keep-alive\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


app = create_app()
