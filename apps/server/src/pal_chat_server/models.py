from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


class ConversationStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    RUNNING = "running"
    PAUSED = "paused"
    ENDED = "ended"


class ProfileTemplateRecord(Base):
    __tablename__ = "profile_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CredentialRecord(Base):
    __tablename__ = "credentials"

    credential_ref: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(255))
    masked_value: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ConversationRecord(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32))
    draft_profile_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    locked_profile_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    profile_hash: Mapped[str | None] = mapped_column(String(64))
    guardrails_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    catalog_metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    validation_issues_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    archive_dir: Mapped[str | None] = mapped_column(String(512))
    manifest_path: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
