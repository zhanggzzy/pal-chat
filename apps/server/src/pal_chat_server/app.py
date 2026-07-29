from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pal_chat_server.agent_runtime import is_agent_runtime_enabled
from pal_chat_server.config import Settings, get_settings
from pal_chat_server.db import (
    ensure_catalog_schema,
    get_db_session,
    get_engine,
    get_session_factory,
)
from pal_chat_server.errors import AppError, build_error_response
from pal_chat_server.models import ConversationRecord, ConversationStatus
from pal_chat_server.schemas import (
    BootstrapResponse,
    CatalogMetadataPatchRequest,
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationRead,
    CpRevisionListResponse,
    CpRevisionRead,
    CredentialCreateRequest,
    CredentialListResponse,
    CredentialValidateResponse,
    DraftProfileUpdateRequest,
    ErrorEnvelope,
    ExperimentProfile,
    GuardrailPatchRequest,
    HealthResponse,
    MessageCommitResponse,
    MessageListResponse,
    MessageRead,
    MessageSubmitRequest,
    ProfileCloneResponse,
    ProfileTemplateCreateRequest,
    ProfileTemplateListResponse,
    ProfileTemplateRead,
    ProfileTemplateUpdateRequest,
    ValidationResult,
)
from pal_chat_server.sequence_runtime import (
    SOCKET_MANAGER,
    MessagePayload,
    commit_message,
    connect_transcript,
    dispatch_pending_outbox,
    get_cp_revision,
    list_cp_revisions,
    list_messages,
    replay_events,
    retry_submission,
    session_snapshot,
)
from pal_chat_server.services import (
    bootstrap_payload,
    clone_template_profile,
    create_conversation,
    create_credential,
    create_template,
    delete_credential,
    delete_template,
    end_conversation,
    ensure_default_template,
    get_conversation_detail,
    get_conversation_or_404,
    get_template_or_404,
    list_conversations,
    list_credentials,
    list_templates,
    patch_catalog_metadata,
    patch_guardrails,
    pause_conversation,
    resume_conversation,
    start_conversation,
    sync_catalog_from_manifests,
    to_template_read,
    update_draft_profile,
    update_template,
    validate_conversation,
    validate_credential,
    validate_profile,
)
from pal_chat_server.worker_supervisor import INTERNAL_SOCKET_MANAGER, WorkerSupervisor


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_engine(get_settings())
    yield


def _catalog_bootstrap(db: Session, settings: Settings) -> None:
    ensure_default_template(db)
    sync_catalog_from_manifests(db, settings)


def create_app() -> FastAPI:
    settings = get_settings()
    worker_supervisor = WorkerSupervisor(server_base_url=settings.server_base_url)
    app = FastAPI(
        title="pal-chat API",
        version=settings.app_version,
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
    app.state.worker_supervisor = worker_supervisor

    def conversation_profile(conversation: ConversationRecord) -> ExperimentProfile:
        locked = getattr(conversation, "locked_profile_json", None)
        draft = getattr(conversation, "draft_profile_json", None)
        source = locked or draft
        if source is None:
            raise AppError(
                code="profile_missing",
                status_code=409,
                message="Conversation profile is missing.",
            )
        return ExperimentProfile.model_validate(source)

    def get_worker_supervisor() -> WorkerSupervisor:
        return app.state.worker_supervisor  # type: ignore[no-any-return]

    def use_process_workers(profile: ExperimentProfile) -> bool:
        return get_worker_supervisor().has_process_mode(profile)

    def ensure_process_runtime_available(profile: ExperimentProfile) -> None:
        if is_agent_runtime_enabled(profile) and not use_process_workers(profile):
            raise AppError(
                code="worker_runtime_unavailable",
                status_code=409,
                message="Agent runtime requires configured worker processes.",
            )

    def publish_runtime_event(conversation_id: str, event: dict[str, Any]) -> None:
        get_worker_supervisor().publish_public_event(conversation_id, event)

    def internal_auth_tuple(request: Request) -> tuple[str, str, str]:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise AppError(
                code="internal_auth_failed",
                status_code=401,
                message="Missing internal bearer token.",
            )
        token = authorization.removeprefix("Bearer ").strip()
        agent_id = request.headers.get("X-Agent-ID", "")
        profile_hash = request.headers.get("X-Profile-Hash", "")
        if not token or not agent_id or not profile_hash:
            raise AppError(
                code="internal_auth_failed",
                status_code=401,
                message="Incomplete internal auth tuple.",
            )
        return token, agent_id, profile_hash

    def ensure_conversation_write_allowed(conversation_status: str) -> None:
        if conversation_status == ConversationStatus.RUNNING.value:
            return
        if conversation_status == ConversationStatus.ENDED.value:
            raise AppError(
                code="conversation_read_only",
                status_code=409,
                message="Ended conversations are read-only.",
            )
        raise AppError(
            code="conversation_not_running",
            status_code=409,
            message="Conversation must be running before accepting messages.",
        )

    def payload_list(payload: dict[str, object], key: str) -> list[object]:
        value = payload.get(key, [])
        if not isinstance(value, list):
            raise AppError(
                code="invalid_internal_payload",
                status_code=422,
                message=f"{key} must be a list.",
            )
        return value

    def payload_optional_int(payload: dict[str, object], key: str) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        try:
            return int(cast(int | str | float, value))
        except (TypeError, ValueError) as exc:
            raise AppError(
                code="invalid_internal_payload",
                status_code=422,
                message=f"{key} must be an integer.",
            ) from exc

    def payload_required_int(payload: dict[str, object], key: str) -> int:
        value = payload_optional_int(payload, key)
        if value is None:
            raise AppError(
                code="invalid_internal_payload",
                status_code=422,
                message=f"{key} is required.",
            )
        return value

    def typing_status(action: object) -> str:
        return "active" if action == "start" else "idle"

    def restart_count_for(
        conversation: ConversationRecord,
        *,
        profile: ExperimentProfile,
        agent_id: str,
    ) -> int:
        snapshot = get_worker_supervisor().runtime_snapshot(
            conversation,
            profile=profile,
        )
        workers = cast(list[dict[str, object]], snapshot["workers"])
        for worker in workers:
            if worker.get("agent_id") == agent_id:
                return int(cast(int | str, worker.get("restart_count", 0)))
        return 0

    def is_terminal_run_status(status_name: str) -> bool:
        return status_name in {
            "BUDGET_EXHAUSTED",
            "COMMITTED",
            "FATAL",
            "INVALIDATED",
            "PAUSED",
            "SILENT",
        }

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
    def ready_health() -> HealthResponse:
        engine = get_engine(settings)
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
                details={
                    "expected": settings.ready_schema_revision,
                    "actual": revision,
                },
            )
        return HealthResponse(status="ready", schema_revision=revision)

    @app.get("/api/v1/bootstrap", response_model=BootstrapResponse, tags=["bootstrap"])
    def get_bootstrap(db: Session = Depends(get_db_session)) -> BootstrapResponse:
        _catalog_bootstrap(db, settings)
        return BootstrapResponse.model_validate(bootstrap_payload(db, settings=settings))

    @app.get(
        "/api/v1/credentials",
        response_model=CredentialListResponse,
        tags=["credentials"],
    )
    def get_credentials(db: Session = Depends(get_db_session)) -> CredentialListResponse:
        _catalog_bootstrap(db, settings)
        return CredentialListResponse(items=list_credentials(db))

    @app.post(
        "/api/v1/credentials",
        response_model=CredentialValidateResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["credentials"],
    )
    def post_credentials(
        payload: CredentialCreateRequest,
        db: Session = Depends(get_db_session),
    ) -> CredentialValidateResponse:
        _catalog_bootstrap(db, settings)
        credential = create_credential(
            db,
            settings=settings,
            provider=payload.provider,
            label=payload.label,
            secret=payload.secret,
        )
        return CredentialValidateResponse(credential=credential, ok=True)

    @app.delete(
        "/api/v1/credentials/{credential_ref}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["credentials"],
    )
    def remove_credential(
        credential_ref: str,
        db: Session = Depends(get_db_session),
    ) -> Response:
        _catalog_bootstrap(db, settings)
        delete_credential(db, settings=settings, credential_ref=credential_ref)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/v1/credentials/{credential_ref}/validate",
        response_model=CredentialValidateResponse,
        tags=["credentials"],
    )
    def post_credential_validate(
        credential_ref: str,
        db: Session = Depends(get_db_session),
    ) -> CredentialValidateResponse:
        _catalog_bootstrap(db, settings)
        credential, ok = validate_credential(
            db,
            settings=settings,
            credential_ref=credential_ref,
        )
        return CredentialValidateResponse(credential=credential, ok=ok)

    @app.get(
        "/api/v1/profile-templates",
        response_model=ProfileTemplateListResponse,
        tags=["profile-templates"],
    )
    def get_profile_templates(
        db: Session = Depends(get_db_session),
    ) -> ProfileTemplateListResponse:
        _catalog_bootstrap(db, settings)
        return ProfileTemplateListResponse(items=list_templates(db))

    @app.post(
        "/api/v1/profile-templates",
        response_model=ProfileTemplateRead,
        status_code=status.HTTP_201_CREATED,
        tags=["profile-templates"],
    )
    def post_profile_template(
        payload: ProfileTemplateCreateRequest,
        db: Session = Depends(get_db_session),
    ) -> ProfileTemplateRead:
        _catalog_bootstrap(db, settings)
        return create_template(
            db,
            slug=payload.slug,
            title=payload.title,
            description=payload.description,
            profile=payload.profile,
        )

    @app.get(
        "/api/v1/profile-templates/{template_id}",
        response_model=ProfileTemplateRead,
        tags=["profile-templates"],
    )
    def get_profile_template(
        template_id: str,
        db: Session = Depends(get_db_session),
    ) -> ProfileTemplateRead:
        _catalog_bootstrap(db, settings)
        return to_template_read(get_template_or_404(db, template_id))

    @app.put(
        "/api/v1/profile-templates/{template_id}",
        response_model=ProfileTemplateRead,
        tags=["profile-templates"],
    )
    def put_profile_template(
        template_id: str,
        payload: ProfileTemplateUpdateRequest,
        db: Session = Depends(get_db_session),
    ) -> ProfileTemplateRead:
        _catalog_bootstrap(db, settings)
        return update_template(
            db,
            template_id=template_id,
            title=payload.title,
            description=payload.description,
            profile=payload.profile,
        )

    @app.delete(
        "/api/v1/profile-templates/{template_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        tags=["profile-templates"],
    )
    def remove_profile_template(
        template_id: str,
        db: Session = Depends(get_db_session),
    ) -> Response:
        _catalog_bootstrap(db, settings)
        delete_template(db, template_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/api/v1/profile-templates/{template_id}/clone",
        response_model=ProfileCloneResponse,
        tags=["profile-templates"],
    )
    def clone_profile_template(
        template_id: str,
        db: Session = Depends(get_db_session),
    ) -> ProfileCloneResponse:
        _catalog_bootstrap(db, settings)
        return ProfileCloneResponse(profile=clone_template_profile(db, template_id))

    @app.post(
        "/api/v1/profile-templates/{template_id}/validate",
        response_model=ValidationResult,
        tags=["profile-templates"],
    )
    def validate_profile_template(
        template_id: str,
        db: Session = Depends(get_db_session),
    ) -> ValidationResult:
        _catalog_bootstrap(db, settings)
        template = get_template_or_404(db, template_id)
        return validate_profile(
            db,
            profile=ExperimentProfile.model_validate(template.profile_json),
            settings=settings,
        )

    @app.get(
        "/api/v1/conversations",
        response_model=ConversationListResponse,
        tags=["conversations"],
    )
    def get_conversations(db: Session = Depends(get_db_session)) -> ConversationListResponse:
        _catalog_bootstrap(db, settings)
        return ConversationListResponse(items=list_conversations(db))

    @app.post(
        "/api/v1/conversations",
        response_model=ConversationRead,
        status_code=status.HTTP_201_CREATED,
        tags=["conversations"],
    )
    def post_conversation(
        payload: ConversationCreateRequest,
        db: Session = Depends(get_db_session),
    ) -> ConversationRead:
        _catalog_bootstrap(db, settings)
        return create_conversation(
            db,
            title=payload.title,
            template_id=payload.profile_template_id,
            draft_profile=payload.draft_profile,
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}",
        response_model=ConversationDetailResponse,
        tags=["conversations"],
    )
    def get_conversation(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ConversationDetailResponse:
        _catalog_bootstrap(db, settings)
        conversation, validation, manifest = get_conversation_detail(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )
        return ConversationDetailResponse(
            conversation=conversation,
            validation=validation,
            manifest=manifest,
        )

    @app.put(
        "/api/v1/conversations/{conversation_id}/draft-profile",
        response_model=ConversationRead,
        tags=["conversations"],
    )
    def put_draft_profile(
        conversation_id: str,
        payload: DraftProfileUpdateRequest,
        db: Session = Depends(get_db_session),
    ) -> ConversationRead:
        _catalog_bootstrap(db, settings)
        return update_draft_profile(
            db,
            conversation_id=conversation_id,
            draft_profile=payload.draft_profile,
        )

    @app.post(
        "/api/v1/conversations/{conversation_id}/validate",
        response_model=ValidationResult,
        tags=["conversations"],
    )
    def post_conversation_validate(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ValidationResult:
        _catalog_bootstrap(db, settings)
        return validate_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )

    @app.post(
        "/api/v1/conversations/{conversation_id}/start",
        response_model=ConversationRead,
        tags=["conversations"],
    )
    def post_conversation_start(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ConversationRead:
        _catalog_bootstrap(db, settings)
        conversation = start_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )
        record = get_conversation_or_404(db, conversation_id)
        profile = conversation_profile(record)
        ensure_process_runtime_available(profile)
        if use_process_workers(profile):
            get_worker_supervisor().start_workers(
                record,
                profile=profile,
                guardrails=record.guardrails_json,
            )
        return conversation

    @app.post(
        "/api/v1/conversations/{conversation_id}/pause",
        response_model=ConversationRead,
        tags=["conversations"],
    )
    def post_conversation_pause(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ConversationRead:
        _catalog_bootstrap(db, settings)
        conversation = pause_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )
        record = get_conversation_or_404(db, conversation_id)
        profile = conversation_profile(record)
        ensure_process_runtime_available(profile)
        if use_process_workers(profile):
            get_worker_supervisor().pause_workers(conversation_id)
        return conversation

    @app.post(
        "/api/v1/conversations/{conversation_id}/resume",
        response_model=ConversationRead,
        tags=["conversations"],
    )
    def post_conversation_resume(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ConversationRead:
        _catalog_bootstrap(db, settings)
        conversation = resume_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )
        record = get_conversation_or_404(db, conversation_id)
        profile = conversation_profile(record)
        ensure_process_runtime_available(profile)
        if use_process_workers(profile):
            get_worker_supervisor().resume_workers(conversation_id)
        return conversation

    @app.post(
        "/api/v1/conversations/{conversation_id}/end",
        response_model=ConversationRead,
        tags=["conversations"],
    )
    def post_conversation_end(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ConversationRead:
        _catalog_bootstrap(db, settings)
        conversation = end_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )
        record = get_conversation_or_404(db, conversation_id)
        profile = conversation_profile(record)
        ensure_process_runtime_available(profile)
        if use_process_workers(profile):
            get_worker_supervisor().stop_workers(conversation_id)
        return conversation

    @app.post(
        "/api/v1/conversations/{conversation_id}/clone-profile",
        response_model=ProfileCloneResponse,
        tags=["conversations"],
    )
    def post_conversation_clone_profile(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ProfileCloneResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        return ProfileCloneResponse(
            profile=ExperimentProfile.model_validate(
                conversation.draft_profile_json
            )
        )

    @app.patch(
        "/api/v1/conversations/{conversation_id}/guardrails",
        response_model=ConversationRead,
        tags=["conversations"],
    )
    def patch_conversation_guardrails(
        conversation_id: str,
        payload: GuardrailPatchRequest,
        db: Session = Depends(get_db_session),
    ) -> ConversationRead:
        _catalog_bootstrap(db, settings)
        patch = payload.model_dump(mode="json", exclude_none=True)
        return patch_guardrails(
            db,
            conversation_id=conversation_id,
            patch=patch,
            settings=settings,
        )

    @app.patch(
        "/api/v1/conversations/{conversation_id}/catalog-metadata",
        response_model=ConversationRead,
        tags=["conversations"],
    )
    def patch_conversation_catalog_metadata(
        conversation_id: str,
        payload: CatalogMetadataPatchRequest,
        db: Session = Depends(get_db_session),
    ) -> ConversationRead:
        _catalog_bootstrap(db, settings)
        return patch_catalog_metadata(
            db,
            conversation_id=conversation_id,
            metadata=payload.metadata,
            settings=settings,
        )

    @app.post(
        "/api/v1/conversations/{conversation_id}/messages",
        response_model=MessageCommitResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["messages"],
    )
    def post_conversation_message(
        conversation_id: str,
        payload: MessageSubmitRequest,
        db: Session = Depends(get_db_session),
    ) -> MessageCommitResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        ensure_conversation_write_allowed(conversation.status)
        message, cp_revision, _ = commit_message(
            conversation,
            profile=conversation_profile(conversation),
            payload=MessagePayload(**payload.model_dump()),
        )
        profile = conversation_profile(conversation)
        ensure_process_runtime_available(profile)
        if use_process_workers(profile):
            publish_runtime_event(
                conversation.id,
                {
                    "protocol_version": 1,
                    "event_id": str(message["message_id"]),
                    "event_type": "message.committed",
                    "conversation_id": conversation.id,
                    "conversation_seq": message["conversation_seq"],
                    "payload": {"message": message},
                },
            )
        return MessageCommitResponse(
            message=MessageRead.model_validate(message),
            cp_revision=CpRevisionRead.model_validate(cp_revision),
        )

    @app.post(
        "/api/v1/conversations/{conversation_id}/submissions/{client_message_id}/retry",
        response_model=MessageCommitResponse,
        tags=["messages"],
    )
    def post_retry_submission(
        conversation_id: str,
        client_message_id: str,
        db: Session = Depends(get_db_session),
    ) -> MessageCommitResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        ensure_conversation_write_allowed(conversation.status)
        message, cp_revision, _ = retry_submission(
            conversation,
            profile=conversation_profile(conversation),
            client_message_id=client_message_id,
        )
        profile = conversation_profile(conversation)
        ensure_process_runtime_available(profile)
        if use_process_workers(profile):
            publish_runtime_event(
                conversation.id,
                {
                    "protocol_version": 1,
                    "event_id": str(message["message_id"]),
                    "event_type": "message.committed",
                    "conversation_id": conversation.id,
                    "conversation_seq": message["conversation_seq"],
                    "payload": {"message": message},
                },
            )
        return MessageCommitResponse(
            message=MessageRead.model_validate(message),
            cp_revision=CpRevisionRead.model_validate(cp_revision),
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/messages",
        response_model=MessageListResponse,
        tags=["messages"],
    )
    def get_conversation_messages(
        conversation_id: str,
        after_seq: int = 0,
        limit: int = 200,
        db: Session = Depends(get_db_session),
    ) -> MessageListResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        if conversation.archive_dir is None:
            raise AppError(
                code="archive_missing",
                status_code=409,
                message="Conversation archive is missing.",
            )
        items = list_messages(
            conversation,
            after_seq=max(after_seq, 0),
            limit=max(1, min(limit, 500)),
        )
        return MessageListResponse(items=[MessageRead.model_validate(item) for item in items])

    @app.get(
        "/api/v1/conversations/{conversation_id}/cp-revisions",
        response_model=CpRevisionListResponse,
        tags=["cp"],
    )
    def get_conversation_cp_revisions(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> CpRevisionListResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        if conversation.archive_dir is None:
            raise AppError(
                code="archive_missing",
                status_code=409,
                message="Conversation archive is missing.",
            )
        items = list_cp_revisions(conversation)
        return CpRevisionListResponse(items=[CpRevisionRead.model_validate(item) for item in items])

    @app.get(
        "/api/v1/conversations/{conversation_id}/cp-revisions/{projection_revision}",
        response_model=CpRevisionRead,
        tags=["cp"],
    )
    def get_conversation_cp_revision(
        conversation_id: str,
        projection_revision: int,
        db: Session = Depends(get_db_session),
    ) -> CpRevisionRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        if conversation.archive_dir is None:
            raise AppError(
                code="archive_missing",
                status_code=409,
                message="Conversation archive is missing.",
            )
        return CpRevisionRead.model_validate(get_cp_revision(conversation, projection_revision))

    @app.websocket("/ws/v1/conversations/{conversation_id}")
    async def conversation_events(
        websocket: WebSocket,
        conversation_id: str,
        after_seq: int = 0,
    ) -> None:
        ensure_catalog_schema(settings)
        session_factory = get_session_factory()
        with session_factory() as db:
            _catalog_bootstrap(db, settings)
            conversation = get_conversation_or_404(db, conversation_id)
            if conversation.archive_dir is None:
                await websocket.close(code=4409, reason="Conversation archive is missing.")
                return
        await SOCKET_MANAGER.connect(conversation_id, websocket)
        try:
            await websocket.send_json(session_snapshot(conversation))
            for event in replay_events(conversation, after_seq=max(after_seq, 0)):
                await websocket.send_json(event)
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            SOCKET_MANAGER.disconnect(conversation_id, websocket)

    @app.get(
        "/internal/v1/conversations/{conversation_id}/messages",
        tags=["internal"],
    )
    def get_internal_messages(
        conversation_id: str,
        request: Request,
        after_seq: int = 0,
        db: Session = Depends(get_db_session),
    ) -> dict[str, object]:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        token, agent_id, profile_hash = internal_auth_tuple(request)
        get_worker_supervisor().verify(
            conversation_id=conversation_id,
            token=token,
            agent_id=agent_id,
            profile_hash=profile_hash,
        )
        return {"items": list_messages(conversation, after_seq=max(after_seq, 0), limit=500)}

    @app.get(
        "/internal/v1/conversations/{conversation_id}/runtime-snapshot",
        tags=["internal"],
    )
    def get_internal_runtime_snapshot(
        conversation_id: str,
        request: Request,
        db: Session = Depends(get_db_session),
    ) -> dict[str, object]:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        profile = conversation_profile(conversation)
        token, agent_id, profile_hash = internal_auth_tuple(request)
        get_worker_supervisor().verify(
            conversation_id=conversation_id,
            token=token,
            agent_id=agent_id,
            profile_hash=profile_hash,
        )
        snapshot = get_worker_supervisor().runtime_snapshot(
            conversation,
            profile=profile,
        )
        snapshot.update(
            {
                "agent_id": agent_id,
                "all_agent_ids": [profile.agent_a.agent_id, profile.agent_b.agent_id],
                "latest_conversation_seq": session_snapshot(conversation)["payload"][
                    "latest_conversation_seq"
                ],
                "profile": profile.model_dump(mode="json"),
                "worker_restart_limit": conversation.guardrails_json["worker_restart_limit"],
                "state_dir": str(Path(conversation.archive_dir or ".") / "module-state" / agent_id),
            }
        )
        return snapshot

    @app.post(
        "/internal/v1/conversations/{conversation_id}/agent-actions",
        tags=["internal"],
    )
    def post_internal_agent_action(
        conversation_id: str,
        payload: dict[str, object],
        request: Request,
        db: Session = Depends(get_db_session),
    ) -> dict[str, object]:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        ensure_conversation_write_allowed(conversation.status)
        profile = conversation_profile(conversation)
        token, agent_id, profile_hash = internal_auth_tuple(request)
        get_worker_supervisor().verify(
            conversation_id=conversation_id,
            token=token,
            agent_id=agent_id,
            profile_hash=profile_hash,
        )
        message, cp_revision, _ = commit_message(
            conversation,
            profile=profile,
            payload=MessagePayload(
                client_message_id=str(payload["run_id"]),
                content_markdown=str(payload["content_markdown"]),
                mentions=[str(item) for item in payload_list(payload, "mentions")],
                primary_reply_to=(
                    str(payload["primary_reply_to"])
                    if payload.get("primary_reply_to") is not None
                    else None
                ),
                responds_to=[str(item) for item in payload_list(payload, "responds_to")],
                sender_kind="agent",
                sender_id=agent_id,
                expected_conversation_seq=payload_required_int(
                    payload,
                    "expected_conversation_seq",
                ),
                idempotency_key=str(payload["idempotency_key"]),
                causal_episode_id=(
                    str(payload["causal_episode_id"])
                    if payload.get("causal_episode_id") is not None
                    else None
                ),
                caused_by_message_id=(
                    str(payload["caused_by_message_id"])
                    if payload.get("caused_by_message_id") is not None
                    else None
                ),
                agent_hop=payload_optional_int(payload, "agent_hop") or 0,
            ),
        )
        publish_runtime_event(
            conversation.id,
            {
                "protocol_version": 1,
                "event_id": str(message["message_id"]),
                "event_type": "message.committed",
                "conversation_id": conversation.id,
                "conversation_seq": message["conversation_seq"],
                "payload": {"message": message},
            },
        )
        return {"message": message, "cp_revision": cp_revision}

    @app.post(
        "/internal/v1/conversations/{conversation_id}/agent-runs/{run_id}/status",
        tags=["internal"],
    )
    def post_internal_agent_run_status(
        conversation_id: str,
        run_id: str,
        payload: dict[str, object],
        request: Request,
        db: Session = Depends(get_db_session),
    ) -> dict[str, str]:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        token, agent_id, profile_hash = internal_auth_tuple(request)
        get_worker_supervisor().verify(
            conversation_id=conversation_id,
            token=token,
            agent_id=agent_id,
            profile_hash=profile_hash,
        )
        if payload.get("checkpoint_ack") == "paused":
            get_worker_supervisor().record_checkpoint_ack(
                conversation_id=conversation_id,
                agent_id=agent_id,
            )
        with connect_transcript(conversation) as connection:
            profile = conversation_profile(conversation)
            active_run_id = payload.get("active_run_id")
            worker_state = str(payload.get("worker_state", "LISTENING"))
            run_status = str(payload.get("run_status", "RUNNING"))
            connection.execute(
                """
                INSERT INTO agent_runtime_state(
                  agent_id, worker_state, reliable_seq, dirty_since_seq,
                  pending_message_ids_json, pending_root_message_ids_json, active_run_id,
                  typing_status, restart_count, profile_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(agent_id) DO UPDATE SET
                  worker_state = excluded.worker_state,
                  reliable_seq = excluded.reliable_seq,
                  dirty_since_seq = excluded.dirty_since_seq,
                  active_run_id = excluded.active_run_id,
                  typing_status = excluded.typing_status,
                  restart_count = excluded.restart_count,
                  profile_hash = excluded.profile_hash,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    agent_id,
                    worker_state,
                    payload_required_int(payload, "reliable_seq"),
                    payload_optional_int(payload, "dirty_since_seq"),
                    json.dumps([]),
                    json.dumps([]),
                    None if active_run_id in (None, "-") else str(active_run_id),
                    typing_status(payload.get("typing_action")),
                    restart_count_for(
                        conversation,
                        profile=profile,
                        agent_id=agent_id,
                    ),
                    conversation.profile_hash,
                ),
            )
            if run_id != "-":
                connection.execute(
                    """
                    INSERT INTO agent_runs(
                      run_id, agent_id, status, phase, observation_message_ids_json,
                      root_message_ids_json, expected_conversation_seq, profile_hash,
                      idempotency_key, causal_episode_id, caused_by_message_id, agent_hop,
                      decision_json, draft_message_json, earliest_send_at, invalidated_by_seq,
                      error_code, error_message, started_at, updated_at, finished_at
                    ) VALUES (
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                      CASE
                        WHEN ? IN ('COMMITTED', 'INVALIDATED', 'SILENT', 'FATAL', 'PAUSED')
                        THEN CURRENT_TIMESTAMP
                        ELSE NULL
                      END
                    )
                    ON CONFLICT(run_id) DO UPDATE SET
                      status = excluded.status,
                      phase = excluded.phase,
                      expected_conversation_seq = excluded.expected_conversation_seq,
                      causal_episode_id = excluded.causal_episode_id,
                      caused_by_message_id = excluded.caused_by_message_id,
                      agent_hop = excluded.agent_hop,
                      finished_at = CASE
                        WHEN excluded.status IN (
                          'COMMITTED', 'INVALIDATED', 'SILENT', 'FATAL', 'PAUSED'
                        )
                        THEN CURRENT_TIMESTAMP
                        ELSE agent_runs.finished_at
                      END,
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        run_id,
                        agent_id,
                        run_status,
                        worker_state,
                        json.dumps([]),
                        json.dumps([]),
                        payload_required_int(payload, "reliable_seq"),
                        conversation.profile_hash,
                        run_id,
                        payload.get("causal_episode_id"),
                        payload.get("caused_by_message_id"),
                        payload_optional_int(payload, "agent_hop") or 0,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        run_status,
                    ),
                )
            connection.commit()
        return {"ok": "true"}

    @app.post(
        "/internal/v1/conversations/{conversation_id}/agent-runs/{run_id}/typing",
        tags=["internal"],
    )
    def post_internal_agent_typing(
        conversation_id: str,
        run_id: str,
        payload: dict[str, object],
        request: Request,
        db: Session = Depends(get_db_session),
    ) -> dict[str, str]:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        token, agent_id, profile_hash = internal_auth_tuple(request)
        get_worker_supervisor().verify(
            conversation_id=conversation_id,
            token=token,
            agent_id=agent_id,
            profile_hash=profile_hash,
        )
        event_type = (
            "agent.typing_started"
            if payload.get("action") == "start"
            else "agent.typing_stopped"
        )
        with connect_transcript(conversation) as connection:
            connection.execute(
                """
                INSERT INTO outbox_events(
                  event_id, event_type, conversation_seq, payload_json, status,
                  dispatch_attempts, dispatched_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    str(uuid4()),
                    event_type,
                    None,
                    json.dumps(
                        {
                            "agent_id": agent_id,
                            "run_id": None if run_id == "-" else run_id,
                            "reason": payload.get("reason"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "pending",
                    0,
                    None,
                ),
            )
            connection.commit()
        dispatch_pending_outbox(conversation)
        return {"ok": "true"}

    @app.websocket("/internal/v1/conversations/{conversation_id}/agent-stream")
    async def internal_agent_stream(
        websocket: WebSocket,
        conversation_id: str,
        after_seq: int = 0,
    ) -> None:
        token_header = websocket.headers.get("authorization", "")
        token = (
            token_header.removeprefix("Bearer ").strip()
            if token_header.startswith("Bearer ")
            else ""
        )
        agent_id = websocket.headers.get("x-agent-id", "")
        profile_hash = websocket.headers.get("x-profile-hash", "")
        with get_session_factory()() as db:
            _catalog_bootstrap(db, settings)
            conversation = get_conversation_or_404(db, conversation_id)
        get_worker_supervisor().mark_connected(
            conversation_id=conversation_id,
            token=token,
            agent_id=agent_id,
            profile_hash=profile_hash,
        )
        await INTERNAL_SOCKET_MANAGER.connect(conversation_id, agent_id, websocket)
        try:
            await websocket.send_json(session_snapshot(conversation))
            for event in replay_events(conversation, after_seq=max(after_seq, 0)):
                await websocket.send_json(event)
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            INTERNAL_SOCKET_MANAGER.disconnect(conversation_id, agent_id)

    return app


app = create_app()
