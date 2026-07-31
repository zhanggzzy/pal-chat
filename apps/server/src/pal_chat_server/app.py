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
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from pal_chat_server.agent_runtime import is_agent_runtime_enabled
from pal_chat_server.analysis import (
    analysis_export_download_path,
    compute_automatic_metrics,
    create_analysis_export_job,
    get_analysis_export_job,
    get_history_detail,
    list_history_entries,
    load_manual_score,
    resume_analysis_export_jobs,
    save_manual_score,
)
from pal_chat_server.attempts import (
    AdmissionRejectedError,
    InvocationContext,
    InvocationError,
    finalize_attempt,
    invoke_model,
    register_attempt,
)
from pal_chat_server.config import Settings, get_settings
from pal_chat_server.contracts import ModelRequest
from pal_chat_server.db import (
    ensure_catalog_schema,
    get_db_session,
    get_engine,
    get_session_factory,
)
from pal_chat_server.errors import AppError, build_error_response
from pal_chat_server.models import ConversationRecord, ConversationStatus
from pal_chat_server.monitoring import (
    get_cost_breakdown,
    get_memory_revision,
    get_run,
    list_attempts,
    list_causal_episodes,
    list_context_bundles,
    list_logs,
    list_memory_revisions,
    list_runs,
)
from pal_chat_server.schemas import (
    AgentRunListResponse,
    AgentRunRead,
    AnalysisExportJobRead,
    AttemptListResponse,
    AutomaticMetricsRead,
    BootstrapResponse,
    CatalogMetadataPatchRequest,
    CausalEpisodeListResponse,
    CausalEpisodeRead,
    ContextBundleListResponse,
    ContextBundleRead,
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationListResponse,
    ConversationRead,
    CostBreakdownRead,
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
    HistoryDetailResponse,
    HistoryListResponse,
    LogEntryListResponse,
    LogEntryRead,
    ManualScoreRead,
    ManualScoreUpsertRequest,
    MemoryRevisionListResponse,
    MemoryRevisionRead,
    MessageCommitResponse,
    MessageListResponse,
    MessageRead,
    MessageSubmitRequest,
    ProfileCloneResponse,
    ProfileTemplateCreateRequest,
    ProfileTemplateListResponse,
    ProfileTemplateRead,
    ProfileTemplateUpdateRequest,
    RunAttemptRead,
    ValidationResult,
)
from pal_chat_server.sequence_runtime import (
    SOCKET_MANAGER,
    MessagePayload,
    commit_message,
    connect_transcript,
    dispatch_pending_outbox,
    fetch_latest_cp_snapshot,
    get_cp_revision,
    list_cp_revisions,
    list_messages,
    replay_events,
    retry_submission,
    session_snapshot,
    utc_now,
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
    scripted_adapter_from_profile,
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


def _catalog_bootstrap(db: Session, settings: Settings) -> None:
    ensure_default_template(db)
    sync_catalog_from_manifests(db, settings)


def _context_owner_id(context_payload: dict[str, Any]) -> str:
    return str(
        context_payload.get("owner_id") or context_payload.get("run_id") or ""
    )


def create_app() -> FastAPI:
    settings = get_settings()
    worker_supervisor = WorkerSupervisor(server_base_url=settings.server_base_url)

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        ensure_catalog_schema(settings)
        with get_session_factory()() as db:
            ensure_default_template(db)
            sync_catalog_from_manifests(db, settings)
            conversations = list(
                db.execute(
                    select(ConversationRecord).where(ConversationRecord.archive_dir.is_not(None))
                ).scalars()
            )
        for conversation in conversations:
            if conversation.archive_dir is None:
                continue
            dispatch_pending_outbox(conversation)
            resume_analysis_export_jobs(conversation, settings=settings)
        try:
            yield
        finally:
            app_instance.state.worker_supervisor.shutdown()

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

    def enqueue_public_event(
        conversation: ConversationRecord,
        *,
        event_type: str,
        payload: dict[str, Any],
        conversation_seq: int | None = None,
    ) -> None:
        created_at = utc_now().isoformat()
        with connect_transcript(conversation) as connection:
            connection.execute(
                """
                INSERT INTO outbox_events(
                  event_id, event_type, conversation_seq, payload_json, status,
                  dispatch_attempts, dispatched_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    event_type,
                    conversation_seq,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    "pending",
                    0,
                    None,
                    created_at,
                ),
            )
            connection.commit()
        dispatch_pending_outbox(conversation)

    def queue_public_event_row(
        connection: Any,
        *,
        event_type: str,
        payload: dict[str, Any],
        conversation_seq: int | None = None,
    ) -> None:
        created_at = utc_now().isoformat()
        connection.execute(
            """
            INSERT INTO outbox_events(
              event_id, event_type, conversation_seq, payload_json, status,
              dispatch_attempts, dispatched_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                event_type,
                conversation_seq,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "pending",
                0,
                None,
                created_at,
            ),
        )

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

    def current_run_id(value: object) -> str | None:
        if value in (None, "-"):
            return None
        return str(value)

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

    def public_runtime_authority(conversation: ConversationRecord) -> dict[str, Any] | None:
        if conversation.archive_dir is None:
            return None
        with connect_transcript(conversation) as connection:
            rows = connection.execute(
                """
                SELECT agent_id, worker_state, active_run_id, typing_status, typing_run_id,
                       reliable_seq, dirty_since_seq, updated_at
                FROM agent_runtime_state
                ORDER BY agent_id
                """
            ).fetchall()
        agents: dict[str, Any] = {}
        latest_reliable_seq = 0
        for row in rows:
            reliable_seq = int(row["reliable_seq"])
            latest_reliable_seq = max(latest_reliable_seq, reliable_seq)
            agents[str(row["agent_id"])] = {
                "worker_state": str(row["worker_state"]),
                "active_run_id": current_run_id(row["active_run_id"]),
                "typing_status": str(row["typing_status"]),
                "typing_run_id": current_run_id(row["typing_run_id"]),
                "reliable_seq": reliable_seq,
                "dirty_since_seq": (
                    None if row["dirty_since_seq"] is None else int(row["dirty_since_seq"])
                ),
                "updated_at": row["updated_at"],
            }
        return {
            "latest_reliable_seq": latest_reliable_seq,
            "agents": agents,
        }

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

    @app.get(
        "/api/v1/conversations/{conversation_id}/workbench",
        tags=["conversations"],
    )
    def get_conversation_workbench(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> dict[str, Any]:
        _catalog_bootstrap(db, settings)
        conversation, validation, manifest = get_conversation_detail(
            db,
            conversation_id=conversation_id,
            settings=settings,
        )
        runtime_authority = public_runtime_authority(
            get_conversation_or_404(db, conversation_id)
        )
        detail = conversation
        has_archive = detail.archive_dir is not None and detail.status != "draft"
        payload: dict[str, Any] = {
            "detail": {
                "conversation": conversation.model_dump(mode="json"),
                "validation": validation.model_dump(mode="json"),
                "manifest": manifest,
                "runtime_authority": runtime_authority,
            },
            "messages": [],
        }
        if not has_archive:
            return payload
        conversation_record = get_conversation_or_404(db, conversation_id)
        payload["messages"] = list_messages(
            conversation_record,
            after_seq=0,
            limit=1000,
        )
        return payload

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
            runtime_authority=public_runtime_authority(
                get_conversation_or_404(db, conversation_id)
            ),
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
        conversation = get_conversation_or_404(db, conversation_id)
        before = dict(conversation.guardrails_json)
        updated = patch_guardrails(
            db,
            conversation_id=conversation_id,
            patch=patch,
            settings=settings,
        )
        record = get_conversation_or_404(db, conversation_id)
        if record.archive_dir is not None and before != record.guardrails_json:
            changed_fields = {
                key: {"from": before.get(key), "to": record.guardrails_json.get(key)}
                for key in sorted(set(before) | set(record.guardrails_json))
                if before.get(key) != record.guardrails_json.get(key)
            }
            enqueue_public_event(
                record,
                event_type="guardrails.changed",
                payload={"changed_fields": changed_fields, "guardrails": record.guardrails_json},
            )
        return updated

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
            limit=max(1, min(limit, 1000)),
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

    @app.get(
        "/api/v1/conversations/{conversation_id}/runs",
        response_model=AgentRunListResponse,
        tags=["monitoring"],
    )
    def get_conversation_runs(
        conversation_id: str,
        agent_id: str | None = None,
        status_filter: str | None = None,
        limit: int = 100,
        after: str | None = None,
        db: Session = Depends(get_db_session),
    ) -> AgentRunListResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        profile = conversation_profile(conversation)
        items = list_runs(
            conversation,
            profile=profile,
            agent_id=agent_id,
            run_status=status_filter,
            limit=max(1, min(limit, 500)),
            after=after,
        )
        return AgentRunListResponse(items=[AgentRunRead.model_validate(item) for item in items])

    @app.get(
        "/api/v1/conversations/{conversation_id}/runs/{run_id}",
        response_model=AgentRunRead,
        tags=["monitoring"],
    )
    def get_conversation_run(
        conversation_id: str,
        run_id: str,
        db: Session = Depends(get_db_session),
    ) -> AgentRunRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        return AgentRunRead.model_validate(
            get_run(conversation, profile=conversation_profile(conversation), run_id=run_id)
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/agents/{agent_id}/memory/revisions",
        response_model=MemoryRevisionListResponse,
        tags=["monitoring"],
    )
    def get_agent_memory_revisions(
        conversation_id: str,
        agent_id: str,
        db: Session = Depends(get_db_session),
    ) -> MemoryRevisionListResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        items = list_memory_revisions(
            conversation,
            profile=conversation_profile(conversation),
            agent_id=agent_id,
        )
        return MemoryRevisionListResponse(
            items=[MemoryRevisionRead.model_validate(item) for item in items]
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/agents/{agent_id}/memory/revisions/{revision}",
        response_model=MemoryRevisionRead,
        tags=["monitoring"],
    )
    def get_agent_memory_revision(
        conversation_id: str,
        agent_id: str,
        revision: str,
        db: Session = Depends(get_db_session),
    ) -> MemoryRevisionRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        item = get_memory_revision(
            conversation,
            profile=conversation_profile(conversation),
            agent_id=agent_id,
            revision=revision,
        )
        return MemoryRevisionRead.model_validate(item)

    @app.get(
        "/api/v1/conversations/{conversation_id}/logs",
        response_model=LogEntryListResponse,
        tags=["monitoring"],
    )
    def get_conversation_logs(
        conversation_id: str,
        run_id: str | None = None,
        phase: str | None = None,
        level: str | None = None,
        limit: int = 200,
        db: Session = Depends(get_db_session),
    ) -> LogEntryListResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        items = list_logs(
            conversation,
            profile=conversation_profile(conversation),
            run_id=run_id,
            phase=phase,
            level=level,
            limit=max(1, min(limit, 500)),
        )
        return LogEntryListResponse(items=[LogEntryRead.model_validate(item) for item in items])

    @app.get(
        "/api/v1/conversations/{conversation_id}/agents/{agent_id}/context-bundles",
        response_model=ContextBundleListResponse,
        tags=["monitoring"],
    )
    def get_agent_context_bundles(
        conversation_id: str,
        agent_id: str,
        run_id: str | None = None,
        db: Session = Depends(get_db_session),
    ) -> ContextBundleListResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        items = list_context_bundles(
            conversation,
            profile=conversation_profile(conversation),
            agent_id=agent_id,
            run_id=run_id,
        )
        return ContextBundleListResponse(
            items=[ContextBundleRead.model_validate(item) for item in items]
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/agents/{agent_id}/attempts",
        response_model=AttemptListResponse,
        tags=["monitoring"],
    )
    def get_agent_attempts(
        conversation_id: str,
        agent_id: str,
        run_id: str | None = None,
        db: Session = Depends(get_db_session),
    ) -> AttemptListResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        items = list_attempts(
            conversation,
            profile=conversation_profile(conversation),
            agent_id=agent_id,
            run_id=run_id,
        )
        return AttemptListResponse(
            items=[RunAttemptRead.model_validate(item) for item in items]
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/causal-episodes",
        response_model=CausalEpisodeListResponse,
        tags=["monitoring"],
    )
    def get_conversation_causal_episodes(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> CausalEpisodeListResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        items = list_causal_episodes(conversation)
        return CausalEpisodeListResponse(
            items=[CausalEpisodeRead.model_validate(item) for item in items]
        )

    @app.get(
        "/api/v1/conversations/{conversation_id}/metrics/cost-breakdown",
        response_model=CostBreakdownRead,
        tags=["monitoring"],
    )
    def get_conversation_cost_breakdown(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> CostBreakdownRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        payload = get_cost_breakdown(conversation, profile=conversation_profile(conversation))
        return CostBreakdownRead.model_validate(payload)

    @app.get(
        "/api/v1/conversations/{conversation_id}/metrics/automatic",
        response_model=AutomaticMetricsRead,
        tags=["monitoring"],
    )
    def get_conversation_automatic_metrics(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> AutomaticMetricsRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        return AutomaticMetricsRead.model_validate(compute_automatic_metrics(conversation))

    @app.get(
        "/api/v1/conversations/{conversation_id}/manual-score",
        response_model=ManualScoreRead,
        tags=["monitoring"],
    )
    def get_conversation_manual_score(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> ManualScoreRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        return ManualScoreRead.model_validate(load_manual_score(conversation))

    @app.put(
        "/api/v1/conversations/{conversation_id}/manual-score",
        response_model=ManualScoreRead,
        tags=["monitoring"],
    )
    def put_conversation_manual_score(
        conversation_id: str,
        request: ManualScoreUpsertRequest,
        db: Session = Depends(get_db_session),
    ) -> ManualScoreRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        payload = save_manual_score(
            conversation,
            rubric_version=request.rubric_version,
            scores=[item.model_dump(mode="json") for item in request.scores],
            overall_note=request.overall_note,
        )
        return ManualScoreRead.model_validate(payload)

    @app.get(
        "/api/v1/history",
        response_model=HistoryListResponse,
        tags=["monitoring"],
    )
    def get_history_index(
        db: Session = Depends(get_db_session),
    ) -> HistoryListResponse:
        _catalog_bootstrap(db, settings)
        items = list_history_entries(settings)
        return HistoryListResponse.model_validate({"items": items})

    @app.get(
        "/api/v1/history/{conversation_id}",
        response_model=HistoryDetailResponse,
        tags=["monitoring"],
    )
    def get_history_archive(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> HistoryDetailResponse:
        _catalog_bootstrap(db, settings)
        return HistoryDetailResponse.model_validate(get_history_detail(settings, conversation_id))

    def _analysis_job_response(
        conversation_id: str,
        payload: dict[str, Any],
    ) -> AnalysisExportJobRead:
        return AnalysisExportJobRead.model_validate(
            {
                "job_id": payload["job_id"],
                "conversation_id": payload["conversation_id"],
                "status": payload["status"],
                "created_at": payload["created_at"],
                "updated_at": payload["updated_at"],
                "error": payload.get("error"),
                "download_url": (
                    f"/api/v1/conversations/{conversation_id}/analysis-exports/{payload['job_id']}/download"
                    if payload["status"] == "ready"
                    else None
                ),
            }
        )

    @app.post(
        "/api/v1/conversations/{conversation_id}/analysis-exports",
        response_model=AnalysisExportJobRead,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["monitoring"],
    )
    def post_conversation_analysis_export(
        conversation_id: str,
        db: Session = Depends(get_db_session),
    ) -> AnalysisExportJobRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        payload = create_analysis_export_job(conversation, settings=settings)
        return _analysis_job_response(conversation_id, payload)

    @app.get(
        "/api/v1/conversations/{conversation_id}/analysis-exports/{job_id}",
        response_model=AnalysisExportJobRead,
        tags=["monitoring"],
    )
    def get_conversation_analysis_export(
        conversation_id: str,
        job_id: str,
        db: Session = Depends(get_db_session),
    ) -> AnalysisExportJobRead:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        payload = get_analysis_export_job(conversation, job_id)
        return _analysis_job_response(conversation_id, payload)

    @app.get(
        "/api/v1/conversations/{conversation_id}/analysis-exports/{job_id}/download",
        tags=["monitoring"],
    )
    def download_conversation_analysis_export(
        conversation_id: str,
        job_id: str,
        db: Session = Depends(get_db_session),
    ) -> FileResponse:
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        path = analysis_export_download_path(conversation, job_id)
        return FileResponse(path, media_type="application/zip", filename=path.name)

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
        snapshot_payload = session_snapshot(conversation)["payload"]
        reliable_seq = int(snapshot_payload["latest_conversation_seq"])
        with connect_transcript(conversation) as connection:
            runtime_row = connection.execute(
                """
                SELECT reliable_seq
                FROM agent_runtime_state
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            if runtime_row is not None:
                reliable_seq = int(runtime_row["reliable_seq"])
            latest_cp_revision, cp_snapshot = fetch_latest_cp_snapshot(connection)
        snapshot.update(
            {
                "agent_id": agent_id,
                "all_agent_ids": [profile.agent_a.agent_id, profile.agent_b.agent_id],
                "latest_conversation_seq": snapshot_payload["latest_conversation_seq"],
                "reliable_seq": reliable_seq,
                "latest_cp_revision": latest_cp_revision,
                "cp_snapshot": cp_snapshot,
                "profile": profile.model_dump(mode="json"),
                "guardrails": conversation.guardrails_json,
                "worker_restart_limit": conversation.guardrails_json["worker_restart_limit"],
                "state_dir": str(Path(conversation.archive_dir or ".") / "module-state" / agent_id),
            }
        )
        return snapshot

    @app.post(
        "/internal/v1/conversations/{conversation_id}/agent-runs/{run_id}/attempts/register",
        tags=["internal"],
    )
    def post_internal_attempt_register(
        conversation_id: str,
        run_id: str,
        payload: dict[str, object],
        request: Request,
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
        profile = conversation_profile(conversation)
        return register_attempt(
            conversation,
            profile=profile,
            agent_id=agent_id,
            run_id=run_id,
            phase=str(payload.get("phase", "decision")),
            bundle_revision=(
                str(payload["bundle_revision"])
                if payload.get("bundle_revision") is not None
                else None
            ),
            memory_revision_before=(
                str(payload["memory_revision_before"])
                if payload.get("memory_revision_before") is not None
                else None
            ),
            staged_memory_revision=(
                str(payload["staged_memory_revision"])
                if payload.get("staged_memory_revision") is not None
                else None
            ),
            payload=cast(dict[str, Any] | None, payload.get("payload")),
        )

    @app.post(
        "/internal/v1/conversations/{conversation_id}/agent-runs/{run_id}/attempts/{attempt_id}/finalize",
        tags=["internal"],
    )
    def post_internal_attempt_finalize(
        conversation_id: str,
        run_id: str,
        attempt_id: str,
        payload: dict[str, object],
        request: Request,
        db: Session = Depends(get_db_session),
    ) -> dict[str, object]:
        del run_id
        _catalog_bootstrap(db, settings)
        conversation = get_conversation_or_404(db, conversation_id)
        token, agent_id, profile_hash = internal_auth_tuple(request)
        get_worker_supervisor().verify(
            conversation_id=conversation_id,
            token=token,
            agent_id=agent_id,
            profile_hash=profile_hash,
        )
        return finalize_attempt(
            conversation,
            attempt_id=attempt_id,
            status=str(payload.get("status", "failed")),
            payload=cast(dict[str, Any] | None, payload.get("payload")),
            error_code=(
                str(payload["error_code"]) if payload.get("error_code") is not None else None
            ),
            error_message=(
                str(payload["error_message"])
                if payload.get("error_message") is not None
                else None
            ),
            error_class=(
                str(payload["error_class"]) if payload.get("error_class") is not None else None
            ),
            retryable=bool(payload.get("retryable", False)),
            backoff_ms=payload_optional_int(payload, "backoff_ms") or 0,
            prompt_tokens=payload_optional_int(payload, "prompt_tokens") or 0,
            completion_tokens=payload_optional_int(payload, "completion_tokens") or 0,
            total_tokens=payload_optional_int(payload, "total_tokens") or 0,
            cost_usd=(
                float(str(payload["cost_usd"]))
                if payload.get("cost_usd") is not None
                else None
            ),
            memory_revision_after=(
                str(payload["memory_revision_after"])
                if payload.get("memory_revision_after") is not None
                else None
            ),
        )

    @app.post(
        "/internal/v1/conversations/{conversation_id}/model-invocations",
        tags=["internal"],
    )
    def post_internal_model_invocation(
        conversation_id: str,
        payload: dict[str, object],
        request: Request,
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
        profile = conversation_profile(conversation)
        request_payload = cast(dict[str, Any], payload.get("request") or {})
        context_payload = cast(dict[str, Any], payload.get("context") or {})
        model_request = ModelRequest.model_validate(request_payload)
        adapter = scripted_adapter_from_profile(profile)
        repair_enabled = bool(payload.get("repair_enabled", False))
        try:
            result = invoke_model(
                conversation,
                profile=profile,
                adapter=adapter,
                request=model_request,
                context=InvocationContext(
                    owner_kind=str(context_payload.get("owner_kind", "agent_run")),
                    owner_id=_context_owner_id(context_payload),
                    run_id=(
                        str(context_payload["run_id"])
                        if context_payload.get("run_id") is not None
                        else None
                    ),
                    agent_id=(
                        str(context_payload["agent_id"])
                        if context_payload.get("agent_id") is not None
                        else agent_id
                    ),
                    phase=str(context_payload.get("phase", model_request.purpose)),
                    profile_hash=str(context_payload.get("profile_hash", profile_hash)),
                    bundle_revision=(
                        str(context_payload["bundle_revision"])
                        if context_payload.get("bundle_revision") is not None
                        else None
                    ),
                    memory_revision_before=(
                        str(context_payload["memory_revision_before"])
                        if context_payload.get("memory_revision_before") is not None
                        else None
                    ),
                    staged_memory_revision=(
                        str(context_payload["staged_memory_revision"])
                        if context_payload.get("staged_memory_revision") is not None
                        else None
                    ),
                    parent_attempt_id=(
                        str(context_payload["parent_attempt_id"])
                        if context_payload.get("parent_attempt_id") is not None
                        else None
                    ),
                    payload=cast(dict[str, Any] | None, context_payload.get("payload")),
                    estimated_input_tokens=int(context_payload.get("estimated_input_tokens", 0)),
                    max_output_tokens=int(context_payload.get("max_output_tokens", 0)),
                ),
            )
            return {"ok": True, "payload": result, "consumed_call_count": 1}
        except AdmissionRejectedError as exc:
            return {
                "ok": False,
                "admitted": False,
                "error_code": exc.code,
                "error_message": exc.message,
                "consumed_call_count": 0,
            }
        except InvocationError as exc:
            if repair_enabled and exc.code == "INVALID_OUTPUT" and exc.attempt_id is not None:
                repair_request = ModelRequest(
                    purpose="repair",
                    system_prompt="",
                    messages=[
                        {
                            "content": json.dumps(
                                {
                                    "phase": model_request.purpose,
                                    "raw_output_text": exc.payload.get("raw_output_text", ""),
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        }
                    ],
                    metadata={
                        "agent_id": agent_id,
                        "call_index": int(model_request.metadata.get("call_index", 0)) + 1,
                        "fallback_payload": cast(
                            dict[str, Any] | None,
                            model_request.metadata.get("fallback_payload"),
                        )
                        or {},
                    },
                )
                try:
                    repaired = invoke_model(
                        conversation,
                        profile=profile,
                        adapter=adapter,
                        request=repair_request,
                        context=InvocationContext(
                            owner_kind=str(context_payload.get("owner_kind", "agent_run")),
                            owner_id=_context_owner_id(context_payload),
                            run_id=(
                                str(context_payload["run_id"])
                                if context_payload.get("run_id") is not None
                                else None
                            ),
                            agent_id=agent_id,
                            phase="repair",
                            profile_hash=str(context_payload.get("profile_hash", profile_hash)),
                            bundle_revision=(
                                str(context_payload["bundle_revision"])
                                if context_payload.get("bundle_revision") is not None
                                else None
                            ),
                            memory_revision_before=(
                                str(context_payload["memory_revision_before"])
                                if context_payload.get("memory_revision_before") is not None
                                else None
                            ),
                            staged_memory_revision=(
                                str(context_payload["staged_memory_revision"])
                                if context_payload.get("staged_memory_revision") is not None
                                else None
                            ),
                            parent_attempt_id=exc.attempt_id,
                            payload={"repair_of_attempt_id": exc.attempt_id, **exc.payload},
                            estimated_input_tokens=max(
                                1, len(exc.payload.get("raw_output_text", "")) // 4
                            ),
                            max_output_tokens=int(model_request.max_tokens),
                        ),
                    )
                    return {
                        "ok": True,
                        "payload": repaired,
                        "consumed_call_count": 2,
                    }
                except AdmissionRejectedError as repair_exc:
                    return {
                        "ok": False,
                        "admitted": False,
                        "error_code": repair_exc.code,
                        "error_message": repair_exc.message,
                        "consumed_call_count": 1,
                    }
                except InvocationError as repair_err:
                    return {
                        "ok": False,
                        "admitted": True,
                        "error_code": repair_err.code,
                        "error_message": repair_err.message,
                        "error_class": repair_err.error_class,
                        "retryable": repair_err.retryable,
                        "backoff_ms": repair_err.backoff_ms,
                        "consumed_call_count": 2,
                    }
            return {
                "ok": False,
                "admitted": True,
                "error_code": exc.code,
                "error_message": exc.message,
                "error_class": exc.error_class,
                "retryable": exc.retryable,
                "backoff_ms": exc.backoff_ms,
                "consumed_call_count": 1,
            }

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
            state_row = connection.execute(
                """
                SELECT worker_state, active_run_id, typing_status, typing_run_id
                FROM agent_runtime_state
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            run_row = None
            if run_id != "-":
                run_row = connection.execute(
                    """
                    SELECT status, phase, decision_json, draft_message_json
                    FROM agent_runs
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
            profile = conversation_profile(conversation)
            active_run_id = payload.get("active_run_id")
            current_active_run_id = (
                None if state_row is None else current_run_id(state_row["active_run_id"])
            )
            current_typing_status = (
                "idle" if state_row is None else str(state_row["typing_status"])
            )
            current_typing_run_id = (
                None if state_row is None else current_run_id(state_row["typing_run_id"])
            )
            current_worker_state = (
                None if state_row is None else str(state_row["worker_state"])
            )
            worker_state = str(payload.get("worker_state", "LISTENING"))
            run_status = str(payload.get("run_status", "RUNNING"))
            terminal_statuses = {
                "BUDGET_EXHAUSTED",
                "COMMITTED",
                "FATAL",
                "INVALIDATED",
                "PAUSED",
                "SILENT",
            }
            request_run_id = current_run_id(run_id)
            next_active_run_id = (
                None
                if run_status in terminal_statuses or active_run_id in (None, "-")
                else str(active_run_id)
            )
            should_apply_runtime_state = (
                current_active_run_id is None
                if request_run_id is None
                else current_active_run_id in (None, request_run_id)
            )
            next_worker_state = (
                worker_state if should_apply_runtime_state else current_worker_state
            )
            next_runtime_run_id = (
                next_active_run_id if should_apply_runtime_state else current_active_run_id
            )
            next_typing_status = current_typing_status
            next_typing_run_id = current_typing_run_id
            terminal_closes_typing = (
                request_run_id is not None
                and run_status in terminal_statuses
                and current_typing_status == "active"
                and current_typing_run_id == request_run_id
            )
            if should_apply_runtime_state and state_row is None:
                next_typing_status = "idle"
                next_typing_run_id = None
            if terminal_closes_typing:
                next_typing_status = "idle"
                next_typing_run_id = None
            decision_json = payload.get("decision_json")
            draft_message_json = payload.get("draft_message_json")
            error_code = payload.get("error_code")
            error_message = payload.get("error_message")
            connection.execute(
                """
                INSERT INTO agent_runtime_state(
                  agent_id, worker_state, reliable_seq, dirty_since_seq,
                  pending_message_ids_json, pending_root_message_ids_json, active_run_id,
                  typing_status, typing_run_id, restart_count, profile_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(agent_id) DO UPDATE SET
                  worker_state = excluded.worker_state,
                  reliable_seq = excluded.reliable_seq,
                  dirty_since_seq = excluded.dirty_since_seq,
                  active_run_id = excluded.active_run_id,
                  typing_status = excluded.typing_status,
                  typing_run_id = excluded.typing_run_id,
                  restart_count = excluded.restart_count,
                  profile_hash = excluded.profile_hash,
                  updated_at = CURRENT_TIMESTAMP
                """,
                (
                    agent_id,
                    next_worker_state,
                    payload_required_int(payload, "reliable_seq"),
                    payload_optional_int(payload, "dirty_since_seq"),
                    json.dumps([]),
                    json.dumps([]),
                    next_runtime_run_id,
                    next_typing_status,
                    next_typing_run_id,
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
                        WHEN ? IN (
                          'BUDGET_EXHAUSTED', 'COMMITTED', 'INVALIDATED',
                          'SILENT', 'FATAL', 'PAUSED'
                        )
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
                      decision_json = excluded.decision_json,
                      draft_message_json = excluded.draft_message_json,
                      error_code = excluded.error_code,
                      error_message = excluded.error_message,
                      finished_at = CASE
                        WHEN excluded.status IN (
                          'BUDGET_EXHAUSTED', 'COMMITTED', 'INVALIDATED',
                          'SILENT', 'FATAL', 'PAUSED'
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
                        (
                            json.dumps(decision_json, ensure_ascii=False, sort_keys=True)
                            if decision_json is not None
                            else None
                        ),
                        (
                            json.dumps(draft_message_json, ensure_ascii=False, sort_keys=True)
                            if draft_message_json is not None
                            else None
                        ),
                        None,
                        None,
                        None if error_code is None else str(error_code),
                        None if error_message is None else str(error_message),
                        run_status,
                    ),
                )
            if (
                terminal_closes_typing
            ):
                queue_public_event_row(
                    connection,
                    event_type="agent.typing_stopped",
                    payload={
                        "agent_id": agent_id,
                        "run_id": run_id,
                        "reason": run_status.lower(),
                    },
                )
            connection.commit()
        dispatch_pending_outbox(conversation)
        if should_apply_runtime_state and current_worker_state != next_worker_state:
            enqueue_public_event(
                conversation,
                event_type="agent.state_changed",
                payload={
                    "agent_id": agent_id,
                    "from_state": current_worker_state,
                    "to_state": next_worker_state,
                    "run_id": None if run_id == "-" else run_id,
                    "reason": run_status,
                },
            )
        if run_id != "-":
            previous_status = None if run_row is None else run_row["status"]
            previous_phase = None if run_row is None else run_row["phase"]
            previous_decision = None if run_row is None else run_row["decision_json"]
            previous_draft = None if run_row is None else run_row["draft_message_json"]
            next_decision = (
                json.dumps(decision_json, ensure_ascii=False, sort_keys=True)
                if decision_json is not None
                else None
            )
            next_draft = (
                json.dumps(draft_message_json, ensure_ascii=False, sort_keys=True)
                if draft_message_json is not None
                else None
            )
            if (
                previous_status != run_status
                or previous_phase != worker_state
                or previous_decision != next_decision
                or previous_draft != next_draft
            ):
                enqueue_public_event(
                    conversation,
                    event_type="agent.run_updated",
                    payload={
                        "agent_id": agent_id,
                        "run_id": run_id,
                        "status": run_status,
                        "phase": worker_state,
                        "decision_json": decision_json,
                        "draft_message_json": draft_message_json,
                        "causal_episode_id": payload.get("causal_episode_id"),
                        "caused_by_message_id": payload.get("caused_by_message_id"),
                        "agent_hop": payload_optional_int(payload, "agent_hop") or 0,
                    },
                )
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
        desired_typing_status = typing_status(payload.get("action"))
        with connect_transcript(conversation) as connection:
            state_row = connection.execute(
                """
                SELECT active_run_id, typing_status, typing_run_id
                FROM agent_runtime_state
                WHERE agent_id = ?
                """,
                (agent_id,),
            ).fetchone()
            current_active_run_id = (
                None if state_row is None else current_run_id(state_row["active_run_id"])
            )
            current_typing_status = (
                "idle" if state_row is None else str(state_row["typing_status"])
            )
            current_typing_run_id = (
                None if state_row is None else current_run_id(state_row["typing_run_id"])
            )
            request_run_id = current_run_id(run_id)
            if (
                current_typing_status == desired_typing_status
                and current_typing_run_id == request_run_id
            ):
                return {"ok": "true"}
            should_emit = False
            next_typing_run_id = current_typing_run_id
            if desired_typing_status == "active":
                if current_active_run_id != request_run_id or current_typing_status != "idle":
                    return {"ok": "true"}
                next_typing_run_id = request_run_id
                should_emit = True
            else:
                if current_typing_status != "active" or current_typing_run_id != request_run_id:
                    return {"ok": "true"}
                next_typing_run_id = None
                should_emit = True
            if state_row is None or not should_emit:
                return {"ok": "true"}
            connection.execute(
                """
                UPDATE agent_runtime_state
                SET typing_status = ?, typing_run_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE agent_id = ?
                """,
                (desired_typing_status, next_typing_run_id, agent_id),
            )
            queue_public_event_row(
                connection,
                event_type=event_type,
                payload={
                    "agent_id": agent_id,
                    "run_id": None if run_id == "-" else run_id,
                    "reason": payload.get("reason"),
                },
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
