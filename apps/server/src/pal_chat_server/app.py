from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

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


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    get_engine(get_settings())
    yield


def _catalog_bootstrap(db: Session, settings: Settings) -> None:
    ensure_default_template(db)
    sync_catalog_from_manifests(db, settings)


def create_app() -> FastAPI:
    settings = get_settings()
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
        return start_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )

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
        return pause_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )

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
        return resume_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )

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
        return end_conversation(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )

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

    return app


app = create_app()
