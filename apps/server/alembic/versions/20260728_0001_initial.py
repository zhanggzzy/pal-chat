from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260728_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("active_topic_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "participants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("role_prompt", sa.Text(), nullable=True),
        sa.Column("decision_threshold", sa.Float(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "chat_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("root_user_message_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stop_reason", sa.String(length=64), nullable=True),
        sa.Column("max_agent_messages", sa.Integer(), nullable=False),
        sa.Column("max_rounds", sa.Integer(), nullable=False),
        sa.Column("max_total_tokens", sa.Integer(), nullable=False),
        sa.Column("timeout_ms", sa.Integer(), nullable=False),
        sa.Column("agent_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("round_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actual_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cancel_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("author_participant_id", sa.String(length=36), nullable=False),
        sa.Column("parent_message_id", sa.String(length=36), nullable=True),
        sa.Column("caused_by_decision_id", sa.String(length=36), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_participant_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["chat_runs.id"]),
        sa.UniqueConstraint("conversation_id", "sequence_no", name="uq_messages_sequence"),
        sa.UniqueConstraint("caused_by_decision_id", name="uq_messages_caused_by_decision"),
    )
    op.create_table(
        "topics",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("interrupted_topic_id", sa.String(length=36), nullable=True),
        sa.Column("current_summary_revision_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_message_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["interrupted_topic_id"], ["topics.id"]),
    )
    op.create_table(
        "message_topic_links",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("message_id", sa.String(length=36), nullable=False),
        sa.Column("topic_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("route_action", sa.String(length=32), nullable=False),
        sa.Column("route_method", sa.String(length=32), nullable=False),
        sa.Column("signals_json", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("is_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "topic_transitions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("from_topic_id", sa.String(length=36), nullable=True),
        sa.Column("to_topic_id", sa.String(length=36), nullable=False),
        sa.Column("cause_message_id", sa.String(length=36), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("previous_status_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["cause_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_topic_id"], ["topics.id"]),
        sa.ForeignKeyConstraint(["to_topic_id"], ["topics.id"]),
    )
    op.create_table(
        "model_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=False),
        sa.Column("provider_request_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("token_count_source", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pricing_version", sa.String(length=32), nullable=True),
        sa.Column("estimated_cost_micros", sa.Integer(), nullable=True),
        sa.Column("error_category", sa.String(length=32), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["chat_runs.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "topic_summary_revisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("topic_id", sa.String(length=36), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("through_message_id", sa.String(length=36), nullable=False),
        sa.Column("model_call_id", sa.String(length=36), nullable=True),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["model_call_id"], ["model_calls.id"]),
        sa.ForeignKeyConstraint(["through_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "agent_topic_indexes",
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("topic_id", sa.String(length=36), nullable=False),
        sa.Column("salience", sa.Float(), nullable=False),
        sa.Column("role_affinity", sa.Float(), nullable=False),
        sa.Column("familiarity", sa.Float(), nullable=False),
        sa.Column("last_seen_message_id", sa.String(length=36), nullable=True),
        sa.Column("last_selected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("index_version", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["participants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["last_seen_message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id", "topic_id"),
    )
    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_message_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("tokenizer", sa.String(length=64), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("canonical_content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["chat_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_message_id"], ["messages.id"]),
    )
    op.create_table(
        "context_snapshot_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("summary_revision_id", sa.String(length=36), nullable=True),
        sa.Column("rendered_content", sa.Text(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False),
        sa.Column("inclusion_reason", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["snapshot_id"], ["context_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["summary_revision_id"], ["topic_summary_revisions.id"]),
        sa.UniqueConstraint("snapshot_id", "ordinal", name="uq_snapshot_item_ordinal"),
    )
    op.create_table(
        "response_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_message_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("model_call_id", sa.String(length=36), nullable=True),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("should_reply", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("dimensions_json", sa.JSON(), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("reply_intent", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["participants.id"]),
        sa.ForeignKeyConstraint(["model_call_id"], ["model_calls.id"]),
        sa.ForeignKeyConstraint(["run_id"], ["chat_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["context_snapshots.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_message_id"], ["messages.id"]),
        sa.UniqueConstraint("run_id", "trigger_message_id", "agent_id", name="uq_decision_once"),
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("public_payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["chat_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_run_event_seq"),
    )
    op.create_table(
        "request_idempotency",
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id", "key"),
    )

    op.create_index(
        "ix_topics_one_active_per_conversation",
        "topics",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_message_primary_topic",
        "message_topic_links",
        ["message_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'primary'"),
    )
    op.create_index(
        "ix_chat_runs_one_active_per_conversation",
        "chat_runs",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('queued', 'running', 'stop_requested')"),
    )

    op.execute(
        """
        CREATE TRIGGER chat_runs_terminal_guard
        BEFORE UPDATE OF status ON chat_runs
        WHEN OLD.status IN ('stopped', 'failed', 'completed')
         AND NEW.status != OLD.status
        BEGIN
            SELECT RAISE(ABORT, 'chat_runs_terminal_guard');
        END;
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_message_requires_selected_decision
        BEFORE INSERT ON messages
        WHEN NEW.kind = 'agent'
        BEGIN
            SELECT CASE
                WHEN NEW.caused_by_decision_id IS NULL
                THEN RAISE(ABORT, 'agent_message_requires_decision')
            END;
            SELECT CASE
                WHEN (
                    NOT EXISTS (
                        SELECT 1
                        FROM response_decisions
                        JOIN messages AS trigger_messages
                          ON trigger_messages.id = response_decisions.trigger_message_id
                        WHERE response_decisions.id = NEW.caused_by_decision_id
                          AND response_decisions.outcome = 'selected'
                          AND response_decisions.run_id = NEW.run_id
                          AND response_decisions.agent_id = NEW.author_participant_id
                          AND trigger_messages.conversation_id = NEW.conversation_id
                    )
                )
                THEN RAISE(ABORT, 'agent_message_requires_selected_decision')
            END;
        END;
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_message_requires_selected_decision_on_update
        BEFORE UPDATE OF kind, caused_by_decision_id, run_id, author_participant_id, conversation_id
        ON messages
        WHEN NEW.kind = 'agent'
        BEGIN
            SELECT CASE
                WHEN NEW.caused_by_decision_id IS NULL
                THEN RAISE(ABORT, 'agent_message_requires_decision')
            END;
            SELECT CASE
                WHEN (
                    NOT EXISTS (
                        SELECT 1
                        FROM response_decisions
                        JOIN messages AS trigger_messages
                          ON trigger_messages.id = response_decisions.trigger_message_id
                        WHERE response_decisions.id = NEW.caused_by_decision_id
                          AND response_decisions.outcome = 'selected'
                          AND response_decisions.run_id = NEW.run_id
                          AND response_decisions.agent_id = NEW.author_participant_id
                          AND trigger_messages.conversation_id = NEW.conversation_id
                    )
                )
                THEN RAISE(ABORT, 'agent_message_requires_selected_decision')
            END;
        END;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS agent_message_requires_selected_decision_on_update")
    op.execute("DROP TRIGGER IF EXISTS agent_message_requires_selected_decision")
    op.execute("DROP TRIGGER IF EXISTS chat_runs_terminal_guard")
    op.drop_index("ix_chat_runs_one_active_per_conversation", table_name="chat_runs")
    op.drop_index("ix_message_primary_topic", table_name="message_topic_links")
    op.drop_index("ix_topics_one_active_per_conversation", table_name="topics")
    op.drop_table("request_idempotency")
    op.drop_table("run_events")
    op.drop_table("response_decisions")
    op.drop_table("context_snapshot_items")
    op.drop_table("context_snapshots")
    op.drop_table("agent_topic_indexes")
    op.drop_table("topic_summary_revisions")
    op.drop_table("model_calls")
    op.drop_table("topic_transitions")
    op.drop_table("message_topic_links")
    op.drop_table("topics")
    op.drop_table("messages")
    op.drop_table("chat_runs")
    op.drop_table("participants")
    op.drop_table("conversations")
