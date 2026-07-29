from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from pal_chat_server.config import Settings

TRANSCRIPT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS messages (
      message_id TEXT PRIMARY KEY,
      conversation_seq INTEGER UNIQUE NOT NULL,
      sender_kind TEXT NOT NULL,
      sender_id TEXT NOT NULL,
      content_markdown TEXT NOT NULL,
      primary_reply_to TEXT NULL,
      causal_episode_id TEXT NULL,
      caused_by_message_id TEXT NULL,
      agent_hop INTEGER NOT NULL,
      client_message_id TEXT NULL UNIQUE,
      idempotency_key TEXT NULL UNIQUE,
      server_received_at TEXT NOT NULL,
      committed_at TEXT NOT NULL,
      cp_revision INTEGER NOT NULL,
      content_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS message_mentions (
      message_id TEXT NOT NULL,
      member_id TEXT NOT NULL,
      PRIMARY KEY (message_id, member_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS message_response_refs (
      message_id TEXT NOT NULL,
      responds_to_message_id TEXT NOT NULL,
      ordinal INTEGER NOT NULL,
      PRIMARY KEY (message_id, responds_to_message_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cp_revisions (
      projection_revision INTEGER PRIMARY KEY,
      covered_through_seq INTEGER NOT NULL,
      strategy_id TEXT NOT NULL,
      strategy_version TEXT NOT NULL,
      snapshot_json TEXT NOT NULL,
      trace_ref TEXT NOT NULL,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS causal_episode_budget (
      episode_id TEXT PRIMARY KEY,
      root_message_ids_json TEXT NOT NULL,
      total_actions INTEGER NOT NULL,
      agent_a_actions INTEGER NOT NULL,
      agent_b_actions INTEGER NOT NULL,
      max_agent_hop INTEGER NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
)


def checksum_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def ensure_archive_layout(settings: Settings, conversation_id: str) -> Path:
    root = settings.experiments_dir / conversation_id
    (root / "traces" / "contexts").mkdir(parents=True, exist_ok=True)
    (root / "traces" / "provider-responses").mkdir(parents=True, exist_ok=True)
    (root / "module-state" / "server").mkdir(parents=True, exist_ok=True)
    (root / "module-state" / "agent-a").mkdir(parents=True, exist_ok=True)
    (root / "module-state" / "agent-b").mkdir(parents=True, exist_ok=True)
    (root / "visualizations").mkdir(parents=True, exist_ok=True)
    (root / "exports").mkdir(parents=True, exist_ok=True)
    observations = root / "observations.ndjson"
    observations.touch(exist_ok=True)
    transcript = root / "transcript.sqlite"
    with sqlite3.connect(transcript) as connection:
        for statement in TRANSCRIPT_SCHEMA:
            connection.execute(statement)
        connection.commit()
    return root


def append_observation(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def build_file_manifest(root: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for relative in ("manifest.json", "observations.ndjson", "transcript.sqlite"):
        path = root / relative
        if path.exists():
            files.append(
                {
                    "path": relative,
                    "size_bytes": path.stat().st_size,
                    "checksum": checksum_file(path),
                }
            )
    return files


def write_manifest(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    snapshot = dict(payload)
    snapshot["files"] = build_file_manifest(root)
    manifest_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    snapshot["files"] = build_file_manifest(root)
    manifest_path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return snapshot


def scan_manifests(settings: Settings) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not settings.experiments_dir.exists():
        return items
    for manifest_path in sorted(settings.experiments_dir.glob("*/manifest.json")):
        items.append(json.loads(manifest_path.read_text(encoding="utf-8")))
    return items
