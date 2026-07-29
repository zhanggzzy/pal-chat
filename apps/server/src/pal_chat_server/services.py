from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pal_chat_server.archive import (
    append_observation,
    ensure_archive_layout,
    scan_manifests,
    write_manifest,
)
from pal_chat_server.clock import Clock, RealClock
from pal_chat_server.config import Settings
from pal_chat_server.contracts import ModelAdapter
from pal_chat_server.credential_store import mask_secret, secret_store_from_settings
from pal_chat_server.errors import AppError
from pal_chat_server.ids import generate_ulid
from pal_chat_server.models import (
    ConversationRecord,
    ConversationStatus,
    CredentialRecord,
    ProfileTemplateRecord,
)
from pal_chat_server.registry import (
    DEFAULT_MODULE_SELECTIONS,
    DEFAULT_TEMPLATE_ID,
    MODULE_REGISTRY,
    registry_by_id,
)
from pal_chat_server.schemas import (
    AgentProfile,
    ConversationRead,
    CredentialRead,
    ExperimentProfile,
    ModelBinding,
    ModuleSelection,
    ProfileTemplateRead,
    RuntimeGuardrails,
    ValidationIssue,
    ValidationResult,
)

TRANSITION_RULES = [
    ("draft", "validated", "validate"),
    ("draft", "running", "start"),
    ("validated", "running", "start"),
    ("running", "paused", "pause"),
    ("paused", "running", "resume"),
    ("running", "ended", "end"),
    ("paused", "ended", "end"),
]


ALLOWED_TRANSITIONS_BY_ACTION: dict[str, set[tuple[str, str]]] = {}
for from_status, to_status, via in TRANSITION_RULES:
    ALLOWED_TRANSITIONS_BY_ACTION.setdefault(via, set()).add((from_status, to_status))


def utc_now(clock: Clock | None = None) -> datetime:
    return (clock or RealClock()).now_utc()


def default_guardrails() -> RuntimeGuardrails:
    return RuntimeGuardrails()


def default_profile() -> ExperimentProfile:
    modules = {
        kind: ModuleSelection(module_id=module_id)
        for kind, module_id in DEFAULT_MODULE_SELECTIONS.items()
    }
    scripted_model = ModelBinding(provider="scripted", model="scripted://phase1")
    return ExperimentProfile(
        template_id=DEFAULT_TEMPLATE_ID,
        title="Default Natural Chat v1",
        modules=modules,
        agent_a=AgentProfile(
            agent_id="agent-a",
            display_name="Agent A",
            persona_prompt="你是偏分析型成员，优先澄清结构和边界。",
            attention_prior="关注结构化信息、约束和未解决问题。",
            model=scripted_model,
        ),
        agent_b=AgentProfile(
            agent_id="agent-b",
            display_name="Agent B",
            persona_prompt="你是偏综合型成员，优先补充风险和替代方案。",
            attention_prior="关注权衡、风险和行动建议。",
            model=scripted_model,
        ),
        metadata={"phase": 1},
    )


def profile_hash(profile: ExperimentProfile) -> str:
    payload = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return sha256(payload.encode("utf-8")).hexdigest()


def serialize_validation(issues: Iterable[ValidationIssue]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in issues]


def model_credential_refs(profile: ExperimentProfile) -> list[str]:
    refs = [
        binding.credential_ref
        for binding in (profile.agent_a.model, profile.agent_b.model)
        if binding.credential_ref
    ]
    deduped: list[str] = []
    for item in refs:
        if item not in deduped:
            deduped.append(item)
    return deduped


def validate_profile(
    session: Session,
    *,
    profile: ExperimentProfile,
    settings: Settings,
) -> ValidationResult:
    issues: list[ValidationIssue] = []
    registry = registry_by_id()
    capabilities: set[str] = set()
    selected_module_ids = {selection.module_id for selection in profile.modules.values()}

    for kind, selection in profile.modules.items():
        descriptor = registry.get(selection.module_id)
        if descriptor is None:
            issues.append(
                ValidationIssue(
                    code="unknown_module",
                    message=f"Unknown module: {selection.module_id}",
                    path=f"modules.{kind}.module_id",
                )
            )
            continue
        if descriptor.kind != kind:
            issues.append(
                ValidationIssue(
                    code="module_kind_mismatch",
                    message=f"{selection.module_id} is not a {kind} module.",
                    path=f"modules.{kind}.module_id",
                )
            )
            continue
        capabilities.update(descriptor.provides_capabilities)

    for kind, selection in profile.modules.items():
        descriptor = registry.get(selection.module_id)
        if descriptor is None:
            continue
        missing = sorted(set(descriptor.requires_capabilities) - capabilities)
        if missing:
            issues.append(
                ValidationIssue(
                    code="missing_capability",
                    message=f"{selection.module_id} requires capabilities: {', '.join(missing)}",
                    path=f"modules.{kind}",
                )
            )
        for conflict in descriptor.conflicts_with:
            if conflict in selected_module_ids:
                issues.append(
                    ValidationIssue(
                        code="conflicting_module",
                        message=f"{selection.module_id} conflicts with {conflict}",
                        path=f"modules.{kind}",
                    )
                )

    adapter = profile.modules.get("model_adapter")
    if adapter and adapter.module_id == "model.openai-compatible":
        for role, agent in (("agent_a", profile.agent_a), ("agent_b", profile.agent_b)):
            if not agent.model.credential_ref:
                issues.append(
                    ValidationIssue(
                        code="missing_credential_ref",
                        message="OpenAI-compatible binding requires credential_ref.",
                        path=f"{role}.model.credential_ref",
                    )
                )

    for ref in model_credential_refs(profile):
        credential = session.get(CredentialRecord, ref)
        if credential is None:
            issues.append(
                ValidationIssue(
                    code="missing_credential",
                    message=f"Credential {ref} is not configured.",
                    path="credential_ref",
                )
            )
        elif secret_store_from_settings(settings).get_secret(ref) is None:
            issues.append(
                ValidationIssue(
                    code="unresolvable_credential",
                    message=f"Credential {ref} is not available in the secret store.",
                    path="credential_ref",
                )
            )

    if "message_sequence" not in profile.modules or "projection" not in profile.modules:
        issues.append(
            ValidationIssue(
                code="incomplete_profile",
                message="Profile must define message_sequence and projection modules.",
                path="modules",
            )
        )

    return ValidationResult(ok=not issues, issues=issues)


def to_credential_read(record: CredentialRecord) -> CredentialRead:
    return CredentialRead.model_validate(record, from_attributes=True)


def to_template_read(record: ProfileTemplateRecord) -> ProfileTemplateRead:
    payload = {
        "id": record.id,
        "slug": record.slug,
        "title": record.title,
        "description": record.description,
        "profile": record.profile_json,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    return ProfileTemplateRead.model_validate(payload)


def to_conversation_read(record: ConversationRecord) -> ConversationRead:
    payload = {
        "id": record.id,
        "title": record.title,
        "status": record.status,
        "draft_profile": record.draft_profile_json,
        "locked_profile": record.locked_profile_json,
        "profile_hash": record.profile_hash,
        "guardrails": record.guardrails_json,
        "catalog_metadata": record.catalog_metadata_json,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "validated_at": record.validated_at,
        "started_at": record.started_at,
        "ended_at": record.ended_at,
        "archive_dir": record.archive_dir,
        "manifest_path": record.manifest_path,
    }
    return ConversationRead.model_validate(payload)


def ensure_default_template(session: Session, clock: Clock | None = None) -> None:
    existing = session.get(ProfileTemplateRecord, DEFAULT_TEMPLATE_ID)
    if existing is not None:
        return
    now = utc_now(clock)
    template = ProfileTemplateRecord(
        id=DEFAULT_TEMPLATE_ID,
        slug=DEFAULT_TEMPLATE_ID,
        title="Default Natural Chat v1",
        description="Phase 1 frozen profile for deterministic local experiments.",
        profile_json=default_profile().model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )
    session.add(template)
    session.commit()


def sync_catalog_from_manifests(session: Session, settings: Settings) -> None:
    for manifest in scan_manifests(settings):
        conversation_id = manifest["conversation_id"]
        if session.get(ConversationRecord, conversation_id) is not None:
            continue
        now = utc_now()
        record = ConversationRecord(
            id=conversation_id,
            title=manifest["title"],
            status=manifest["status"],
            draft_profile_json=manifest.get("draft_profile") or manifest["locked_profile"],
            locked_profile_json=manifest.get("locked_profile"),
            profile_hash=manifest.get("profile_hash"),
            guardrails_json=manifest["guardrails"],
            catalog_metadata_json=manifest.get("catalog_metadata", {}),
            validation_issues_json=[],
            archive_dir=manifest.get("archive_dir"),
            manifest_path=manifest.get("manifest_path"),
            created_at=datetime.fromisoformat(manifest["created_at"]),
            updated_at=now,
            validated_at=(
                datetime.fromisoformat(manifest["validated_at"])
                if manifest.get("validated_at")
                else None
            ),
            started_at=(
                datetime.fromisoformat(manifest["started_at"])
                if manifest.get("started_at")
                else None
            ),
            ended_at=(
                datetime.fromisoformat(manifest["ended_at"]) if manifest.get("ended_at") else None
            ),
        )
        session.add(record)
    session.commit()


def list_templates(session: Session) -> list[ProfileTemplateRead]:
    items = session.scalars(select(ProfileTemplateRecord)).all()
    return [to_template_read(item) for item in items]


def list_credentials(session: Session) -> list[CredentialRead]:
    return [to_credential_read(item) for item in session.scalars(select(CredentialRecord)).all()]


def create_credential(
    session: Session,
    *,
    settings: Settings,
    provider: str,
    label: str,
    secret: str,
    clock: Clock | None = None,
) -> CredentialRead:
    now = utc_now(clock)
    credential_ref = f"cred_{generate_ulid(now)}"
    secret_store_from_settings(settings).set_secret(credential_ref, secret)
    record = CredentialRecord(
        credential_ref=credential_ref,
        provider=provider,
        label=label,
        masked_value=mask_secret(secret),
        status="configured",
        created_at=now,
        updated_at=now,
        validated_at=None,
        last_error=None,
    )
    session.add(record)
    session.commit()
    return to_credential_read(record)


def delete_credential(session: Session, *, settings: Settings, credential_ref: str) -> None:
    record = session.get(CredentialRecord, credential_ref)
    if record is None:
        raise AppError(
            code="credential_not_found",
            status_code=404,
            message="Credential not found.",
        )
    secret_store_from_settings(settings).delete_secret(credential_ref)
    session.delete(record)
    session.commit()


def validate_credential(
    session: Session,
    *,
    settings: Settings,
    credential_ref: str,
    clock: Clock | None = None,
) -> tuple[CredentialRead, bool]:
    record = session.get(CredentialRecord, credential_ref)
    if record is None:
        raise AppError(
            code="credential_not_found",
            status_code=404,
            message="Credential not found.",
        )
    secret = secret_store_from_settings(settings).get_secret(credential_ref)
    ok = bool(secret)
    now = utc_now(clock)
    record.updated_at = now
    record.validated_at = now if ok else None
    record.status = "validated" if ok else "error"
    record.last_error = None if ok else "Secret missing from credential store."
    session.commit()
    return to_credential_read(record), ok


def create_template(
    session: Session,
    *,
    slug: str,
    title: str,
    description: str,
    profile: ExperimentProfile,
    clock: Clock | None = None,
) -> ProfileTemplateRead:
    existing = session.scalar(
        select(ProfileTemplateRecord).where(ProfileTemplateRecord.slug == slug)
    )
    if existing is not None:
        raise AppError(
            code="template_exists",
            status_code=409,
            message="Template slug already exists.",
        )
    now = utc_now(clock)
    record = ProfileTemplateRecord(
        id=generate_ulid(now),
        slug=slug,
        title=title,
        description=description,
        profile_json=profile.model_dump(mode="json"),
        created_at=now,
        updated_at=now,
    )
    session.add(record)
    session.commit()
    return to_template_read(record)


def get_template_or_404(session: Session, template_id: str) -> ProfileTemplateRecord:
    template = session.get(ProfileTemplateRecord, template_id)
    if template is None:
        raise AppError(
            code="template_not_found",
            status_code=404,
            message="Profile template not found.",
        )
    return template


def update_template(
    session: Session,
    *,
    template_id: str,
    title: str,
    description: str,
    profile: ExperimentProfile,
    clock: Clock | None = None,
) -> ProfileTemplateRead:
    template = get_template_or_404(session, template_id)
    template.title = title
    template.description = description
    template.profile_json = profile.model_dump(mode="json")
    template.updated_at = utc_now(clock)
    session.commit()
    return to_template_read(template)


def delete_template(session: Session, template_id: str) -> None:
    template = get_template_or_404(session, template_id)
    if template.id == DEFAULT_TEMPLATE_ID:
        raise AppError(
            code="default_template_locked",
            status_code=409,
            message="Default template cannot be deleted.",
        )
    session.delete(template)
    session.commit()


def clone_template_profile(session: Session, template_id: str) -> ExperimentProfile:
    return ExperimentProfile.model_validate(get_template_or_404(session, template_id).profile_json)


def get_conversation_or_404(session: Session, conversation_id: str) -> ConversationRecord:
    conversation = session.get(ConversationRecord, conversation_id)
    if conversation is None:
        raise AppError(
            code="conversation_not_found",
            status_code=404,
            message="Conversation not found.",
        )
    return conversation


def _load_profile_from_template(session: Session, template_id: str | None) -> ExperimentProfile:
    template = get_template_or_404(session, template_id or DEFAULT_TEMPLATE_ID)
    return ExperimentProfile.model_validate(template.profile_json)


def list_conversations(session: Session) -> list[ConversationRead]:
    items = session.scalars(select(ConversationRecord)).all()
    return [to_conversation_read(item) for item in items]


def create_conversation(
    session: Session,
    *,
    title: str,
    template_id: str | None,
    draft_profile: ExperimentProfile | None,
    clock: Clock | None = None,
) -> ConversationRead:
    now = utc_now(clock)
    profile = draft_profile or _load_profile_from_template(session, template_id)
    record = ConversationRecord(
        id=generate_ulid(now),
        title=title,
        status=ConversationStatus.DRAFT.value,
        draft_profile_json=profile.model_dump(mode="json"),
        locked_profile_json=None,
        profile_hash=None,
        guardrails_json=default_guardrails().model_dump(mode="json"),
        catalog_metadata_json={},
        validation_issues_json=[],
        archive_dir=None,
        manifest_path=None,
        created_at=now,
        updated_at=now,
        validated_at=None,
        started_at=None,
        ended_at=None,
    )
    session.add(record)
    session.commit()
    return to_conversation_read(record)


def update_draft_profile(
    session: Session,
    *,
    conversation_id: str,
    draft_profile: ExperimentProfile,
    clock: Clock | None = None,
) -> ConversationRead:
    conversation = get_conversation_or_404(session, conversation_id)
    if conversation.started_at is not None:
        raise AppError(
            code="profile_locked",
            status_code=409,
            message="Profile is locked after start.",
        )
    conversation.draft_profile_json = draft_profile.model_dump(mode="json")
    conversation.status = ConversationStatus.DRAFT.value
    conversation.validation_issues_json = []
    conversation.updated_at = utc_now(clock)
    session.commit()
    return to_conversation_read(conversation)


def validate_conversation(
    session: Session,
    *,
    conversation_id: str,
    settings: Settings,
    clock: Clock | None = None,
) -> ValidationResult:
    conversation = get_conversation_or_404(session, conversation_id)
    profile = ExperimentProfile.model_validate(conversation.draft_profile_json)
    result = validate_profile(session, profile=profile, settings=settings)
    conversation.validation_issues_json = serialize_validation(result.issues)
    conversation.updated_at = utc_now(clock)
    if result.ok:
        conversation.status = ConversationStatus.VALIDATED.value
        conversation.validated_at = conversation.updated_at
    session.commit()
    return result


def ensure_no_other_active_conversation(session: Session, current_id: str) -> None:
    active = session.scalars(
        select(ConversationRecord).where(
            ConversationRecord.status.in_(
                [ConversationStatus.RUNNING.value, ConversationStatus.PAUSED.value]
            )
        )
    ).all()
    for item in active:
        if item.id != current_id:
            raise AppError(
                code="conversation_already_active",
                status_code=409,
                message="Only one conversation may be running or paused at a time.",
            )


def ensure_transition_allowed(
    *,
    action: str,
    current_status: str,
    next_status: str,
) -> None:
    allowed = ALLOWED_TRANSITIONS_BY_ACTION.get(action, set())
    if (current_status, next_status) in allowed:
        return
    raise AppError(
        code="invalid_transition",
        status_code=409,
        message=f"Conversation cannot {action} from {current_status} to {next_status}.",
        details={
            "action": action,
            "from_status": current_status,
            "to_status": next_status,
        },
    )


def _credential_refs_payload(session: Session, profile: ExperimentProfile) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for ref in model_credential_refs(profile):
        credential = session.get(CredentialRecord, ref)
        if credential is None:
            continue
        items.append(
            {
                "credential_ref": ref,
                "provider": credential.provider,
                "masked_value": credential.masked_value,
                "status": credential.status,
            }
        )
    return items


def _write_manifest_for_conversation(
    session: Session,
    *,
    settings: Settings,
    conversation: ConversationRecord,
) -> dict[str, Any]:
    if conversation.archive_dir is None:
        raise AppError(
            code="archive_missing",
            status_code=409,
            message="Conversation archive is missing.",
        )
    root = Path(conversation.archive_dir)
    draft_profile = ExperimentProfile.model_validate(conversation.draft_profile_json)
    locked = (
        ExperimentProfile.model_validate(conversation.locked_profile_json)
        if conversation.locked_profile_json
        else None
    )
    payload = {
        "archive_schema_version": 1,
        "conversation_id": conversation.id,
        "title": conversation.title,
        "status": conversation.status,
        "created_at": conversation.created_at.isoformat(),
        "validated_at": (
            conversation.validated_at.isoformat() if conversation.validated_at else None
        ),
        "started_at": conversation.started_at.isoformat() if conversation.started_at else None,
        "ended_at": conversation.ended_at.isoformat() if conversation.ended_at else None,
        "application_version": settings.app_version,
        "profile_hash": conversation.profile_hash,
        "draft_profile": draft_profile.model_dump(mode="json"),
        "locked_profile": locked.model_dump(mode="json") if locked else None,
        "guardrails": conversation.guardrails_json,
        "catalog_metadata": conversation.catalog_metadata_json,
        "agent_profiles": {
            "agent_a": draft_profile.agent_a.model_dump(mode="json"),
            "agent_b": draft_profile.agent_b.model_dump(mode="json"),
        },
        "modules": [item.model_dump(mode="json") for item in MODULE_REGISTRY],
        "prompt_version": draft_profile.prompt_version,
        "credential_refs": _credential_refs_payload(session, draft_profile),
        "final_stats": {"message_count": 0, "cp_revisions": 0},
        "archive_dir": str(root),
        "manifest_path": str(root / "manifest.json"),
    }
    manifest = write_manifest(root, payload)
    conversation.manifest_path = str(root / "manifest.json")
    return manifest


def start_conversation(
    session: Session,
    *,
    conversation_id: str,
    settings: Settings,
    clock: Clock | None = None,
) -> ConversationRead:
    conversation = get_conversation_or_404(session, conversation_id)
    ensure_transition_allowed(
        action="start",
        current_status=conversation.status,
        next_status=ConversationStatus.RUNNING.value,
    )
    ensure_no_other_active_conversation(session, conversation.id)
    result = validate_conversation(
        session,
        conversation_id=conversation.id,
        settings=settings,
        clock=clock,
    )
    if not result.ok:
        raise AppError(
            code="profile_invalid",
            status_code=409,
            message="Conversation profile is invalid.",
        )
    now = utc_now(clock)
    profile = ExperimentProfile.model_validate(conversation.draft_profile_json)
    conversation.status = ConversationStatus.RUNNING.value
    conversation.started_at = conversation.started_at or now
    conversation.updated_at = now
    conversation.locked_profile_json = profile.model_dump(mode="json")
    conversation.profile_hash = profile_hash(profile)
    root = ensure_archive_layout(settings, conversation.id)
    conversation.archive_dir = str(root)
    manifest = _write_manifest_for_conversation(
        session,
        settings=settings,
        conversation=conversation,
    )
    append_observation(
        root / "observations.ndjson",
        {
            "archive_schema_version": 1,
            "record_id": generate_ulid(now),
            "experiment_id": conversation.id,
            "observed_sequence": 1,
            "observed_at_utc": now.isoformat(),
            "observed_at_monotonic_ns": 0,
            "source": "server",
            "destination": "archive",
            "direction": "internal",
            "content_type": "application/json",
            "payload_encoding": "json",
            "payload": {
                "event": "conversation.started",
                "profile_hash": conversation.profile_hash,
                "manifest_path": manifest["manifest_path"],
            },
            "checksum": f"sha256:{conversation.profile_hash}",
        },
    )
    _write_manifest_for_conversation(session, settings=settings, conversation=conversation)
    session.commit()
    return to_conversation_read(conversation)


def pause_conversation(
    session: Session,
    *,
    conversation_id: str,
    settings: Settings,
    clock: Clock | None = None,
) -> ConversationRead:
    conversation = get_conversation_or_404(session, conversation_id)
    ensure_transition_allowed(
        action="pause",
        current_status=conversation.status,
        next_status=ConversationStatus.PAUSED.value,
    )
    conversation.status = ConversationStatus.PAUSED.value
    conversation.updated_at = utc_now(clock)
    if conversation.archive_dir:
        _write_manifest_for_conversation(session, settings=settings, conversation=conversation)
    session.commit()
    return to_conversation_read(conversation)


def resume_conversation(
    session: Session,
    *,
    conversation_id: str,
    settings: Settings,
    clock: Clock | None = None,
) -> ConversationRead:
    conversation = get_conversation_or_404(session, conversation_id)
    ensure_transition_allowed(
        action="resume",
        current_status=conversation.status,
        next_status=ConversationStatus.RUNNING.value,
    )
    ensure_no_other_active_conversation(session, conversation.id)
    conversation.status = ConversationStatus.RUNNING.value
    conversation.updated_at = utc_now(clock)
    if conversation.archive_dir:
        _write_manifest_for_conversation(session, settings=settings, conversation=conversation)
    session.commit()
    return to_conversation_read(conversation)


def end_conversation(
    session: Session,
    *,
    conversation_id: str,
    settings: Settings,
    clock: Clock | None = None,
) -> ConversationRead:
    conversation = get_conversation_or_404(session, conversation_id)
    ensure_transition_allowed(
        action="end",
        current_status=conversation.status,
        next_status=ConversationStatus.ENDED.value,
    )
    now = utc_now(clock)
    conversation.status = ConversationStatus.ENDED.value
    conversation.ended_at = now
    conversation.updated_at = now
    if conversation.archive_dir is None:
        root = ensure_archive_layout(settings, conversation.id)
        conversation.archive_dir = str(root)
    root = Path(conversation.archive_dir)
    append_observation(
        root / "observations.ndjson",
        {
            "archive_schema_version": 1,
            "record_id": generate_ulid(now),
            "experiment_id": conversation.id,
            "observed_sequence": 2,
            "observed_at_utc": now.isoformat(),
            "observed_at_monotonic_ns": 1,
            "source": "server",
            "destination": "archive",
            "direction": "internal",
            "content_type": "application/json",
            "payload_encoding": "json",
            "payload": {"event": "conversation.ended", "status": conversation.status},
            "checksum": f"sha256:{conversation.id}",
        },
    )
    _write_manifest_for_conversation(session, settings=settings, conversation=conversation)
    session.commit()
    return to_conversation_read(conversation)


def patch_guardrails(
    session: Session,
    *,
    conversation_id: str,
    patch: dict[str, Any],
    settings: Settings,
    clock: Clock | None = None,
) -> ConversationRead:
    conversation = get_conversation_or_404(session, conversation_id)
    merged = RuntimeGuardrails.model_validate({**conversation.guardrails_json, **patch})
    conversation.guardrails_json = merged.model_dump(mode="json")
    conversation.updated_at = utc_now(clock)
    if conversation.archive_dir:
        _write_manifest_for_conversation(session, settings=settings, conversation=conversation)
    session.commit()
    return to_conversation_read(conversation)


def patch_catalog_metadata(
    session: Session,
    *,
    conversation_id: str,
    metadata: dict[str, Any],
    settings: Settings,
    clock: Clock | None = None,
) -> ConversationRead:
    conversation = get_conversation_or_404(session, conversation_id)
    conversation.catalog_metadata_json = dict(metadata)
    conversation.updated_at = utc_now(clock)
    if conversation.archive_dir:
        _write_manifest_for_conversation(session, settings=settings, conversation=conversation)
    session.commit()
    return to_conversation_read(conversation)


def get_manifest(conversation: ConversationRecord) -> dict[str, Any] | None:
    if not conversation.manifest_path:
        return None
    path = Path(conversation.manifest_path)
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def get_conversation_detail(
    session: Session,
    *,
    conversation_id: str,
    settings: Settings,
) -> tuple[ConversationRead, ValidationResult, dict[str, Any] | None]:
    conversation = get_conversation_or_404(session, conversation_id)
    issues = [ValidationIssue.model_validate(item) for item in conversation.validation_issues_json]
    validation = ValidationResult(ok=not issues, issues=issues)
    if not issues and conversation.status == ConversationStatus.DRAFT.value:
        profile = ExperimentProfile.model_validate(conversation.draft_profile_json)
        validation = validate_profile(session, profile=profile, settings=settings)
    return to_conversation_read(conversation), validation, get_manifest(conversation)


def bootstrap_payload(
    session: Session,
    *,
    settings: Settings,
) -> dict[str, Any]:
    ensure_default_template(session)
    sync_catalog_from_manifests(session, settings)
    note = None
    # PROJECT_OVERVIEW.md in the issue attachment is dated 2026-07-30, which is in the future
    # relative to the current run date 2026-07-29; expose the absolute date to avoid ambiguity.
    if settings.env == "development":
        note = "Issue baseline document header lists update date 2026-07-30."
    return {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "generated_at": utc_now(),
        "bind_host": settings.bind_host,
        "module_registry": [item.model_dump(mode="json") for item in MODULE_REGISTRY],
        "templates": [item.model_dump(mode="json") for item in list_templates(session)],
        "credentials": [item.model_dump(mode="json") for item in list_credentials(session)],
        "state_transitions": [
            {"from_status": from_status, "to_status": to_status, "via": via}
            for from_status, to_status, via in TRANSITION_RULES
        ],
        "frontend": {
            "dev_command": "npm --prefix apps/web run dev",
            "build_command": "npm --prefix apps/web run build",
        },
        "note": note,
    }


def scripted_adapter_from_profile(profile: ExperimentProfile) -> ModelAdapter:
    from pal_chat_server.scripted_adapter import ScriptedModelAdapter, ScriptedStep

    selection = profile.modules["model_adapter"]
    script = [ScriptedStep.model_validate(item) for item in selection.config.get("script", [])]
    return ScriptedModelAdapter(script)
