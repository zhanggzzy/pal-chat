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
    """
    CREATE TABLE IF NOT EXISTS submissions (
      client_message_id TEXT PRIMARY KEY,
      request_hash TEXT NOT NULL,
      content_markdown TEXT NOT NULL,
      mentions_json TEXT NOT NULL,
      primary_reply_to TEXT NULL,
      responds_to_json TEXT NOT NULL,
      status TEXT NOT NULL,
      error_code TEXT NULL,
      error_message TEXT NULL,
      message_id TEXT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outbox_events (
      event_id TEXT PRIMARY KEY,
      event_type TEXT NOT NULL,
      conversation_seq INTEGER NULL,
      payload_json TEXT NOT NULL,
      status TEXT NOT NULL,
      dispatch_attempts INTEGER NOT NULL DEFAULT 0,
      dispatched_at TEXT NULL,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runtime_state (
      agent_id TEXT PRIMARY KEY,
      worker_state TEXT NOT NULL,
      reliable_seq INTEGER NOT NULL,
      dirty_since_seq INTEGER NULL,
      pending_message_ids_json TEXT NOT NULL DEFAULT '[]',
      pending_root_message_ids_json TEXT NOT NULL DEFAULT '[]',
      active_run_id TEXT NULL,
      typing_status TEXT NOT NULL,
      typing_run_id TEXT NULL,
      restart_count INTEGER NOT NULL DEFAULT 0,
      profile_hash TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runs (
      run_id TEXT PRIMARY KEY,
      agent_id TEXT NOT NULL,
      status TEXT NOT NULL,
      phase TEXT NOT NULL,
      observation_message_ids_json TEXT NOT NULL,
      root_message_ids_json TEXT NOT NULL,
      expected_conversation_seq INTEGER NOT NULL,
      profile_hash TEXT NOT NULL,
      idempotency_key TEXT NOT NULL,
      causal_episode_id TEXT NULL,
      caused_by_message_id TEXT NULL,
      agent_hop INTEGER NOT NULL DEFAULT 0,
      decision_json TEXT NULL,
      draft_message_json TEXT NULL,
      earliest_send_at TEXT NULL,
      invalidated_by_seq INTEGER NULL,
      error_code TEXT NULL,
      error_message TEXT NULL,
      started_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      finished_at TEXT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_attempts (
      attempt_id TEXT PRIMARY KEY,
      owner_kind TEXT NOT NULL,
      owner_id TEXT NOT NULL,
      run_id TEXT NULL,
      agent_id TEXT NULL,
      phase TEXT NOT NULL,
      phase_ordinal INTEGER NOT NULL,
      parent_attempt_id TEXT NULL,
      provider TEXT NOT NULL,
      model TEXT NOT NULL,
      status TEXT NOT NULL,
      profile_hash TEXT NOT NULL,
      bundle_revision TEXT NULL,
      memory_revision_before TEXT NULL,
      memory_revision_after TEXT NULL,
      staged_memory_revision TEXT NULL,
      guardrails_json TEXT NOT NULL,
      payload_json TEXT NULL,
      error_code TEXT NULL,
      error_message TEXT NULL,
      error_class TEXT NULL,
      retryable INTEGER NOT NULL DEFAULT 0,
      backoff_ms INTEGER NOT NULL DEFAULT 0,
      reserved_tokens INTEGER NOT NULL DEFAULT 0,
      prompt_tokens INTEGER NOT NULL DEFAULT 0,
      completion_tokens INTEGER NOT NULL DEFAULT 0,
      total_tokens INTEGER NOT NULL DEFAULT 0,
      cost_usd REAL NOT NULL DEFAULT 0,
      started_at TEXT NOT NULL,
      finished_at TEXT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_llm_attempts_run_id
    ON llm_attempts(run_id, started_at, attempt_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_llm_attempts_agent_id
    ON llm_attempts(agent_id, started_at, attempt_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_admission_events (
      admission_id TEXT PRIMARY KEY,
      owner_kind TEXT NOT NULL,
      owner_id TEXT NOT NULL,
      run_id TEXT NULL,
      agent_id TEXT NULL,
      phase TEXT NOT NULL,
      decision TEXT NOT NULL,
      reason_code TEXT NULL,
      reason_message TEXT NULL,
      reserved_tokens INTEGER NOT NULL DEFAULT 0,
      profile_hash TEXT NOT NULL,
      guardrails_json TEXT NOT NULL,
      payload_json TEXT NULL,
      created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversation_budget_ledger (
      conversation_id TEXT PRIMARY KEY,
      used_llm_calls INTEGER NOT NULL DEFAULT 0,
      used_total_tokens INTEGER NOT NULL DEFAULT 0,
      reserved_total_tokens INTEGER NOT NULL DEFAULT 0,
      used_total_cost_usd REAL NOT NULL DEFAULT 0,
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
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(agent_runtime_state)")
        }
        if "typing_run_id" not in columns:
            connection.execute(
                "ALTER TABLE agent_runtime_state ADD COLUMN typing_run_id TEXT NULL"
            )
        attempt_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(llm_attempts)")
        }
        if attempt_columns:
            if "owner_kind" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE llm_attempts ADD COLUMN owner_kind TEXT NULL"
                )
                connection.execute(
                    "UPDATE llm_attempts SET owner_kind = 'agent_run' WHERE owner_kind IS NULL"
                )
            if "owner_id" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE llm_attempts ADD COLUMN owner_id TEXT NULL"
                )
                connection.execute(
                    "UPDATE llm_attempts SET owner_id = run_id WHERE owner_id IS NULL"
                )
            if "parent_attempt_id" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE llm_attempts ADD COLUMN parent_attempt_id TEXT NULL"
                )
            if "reserved_tokens" not in attempt_columns:
                connection.execute(
                    "ALTER TABLE llm_attempts ADD COLUMN reserved_tokens INTEGER NOT NULL DEFAULT 0"
                )
        budget_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(conversation_budget_ledger)")
        }
        if budget_columns and "reserved_total_tokens" not in budget_columns:
            connection.execute(
                "ALTER TABLE conversation_budget_ledger "
                "ADD COLUMN reserved_total_tokens INTEGER NOT NULL DEFAULT 0"
            )
        connection.commit()
    return root


def append_observation(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def append_observations(path: Path, payloads: list[dict[str, Any]]) -> None:
    if not payloads:
        return
    with path.open("a", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def transcript_path(root: Path) -> Path:
    return root / "transcript.sqlite"


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
    return snapshot


def scan_manifests(settings: Settings) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not settings.experiments_dir.exists():
        return items
    for manifest_path in sorted(settings.experiments_dir.glob("*/manifest.json")):
        items.append(json.loads(manifest_path.read_text(encoding="utf-8")))
    return items
