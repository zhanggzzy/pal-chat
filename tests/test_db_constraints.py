from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pal_chat_server.config import get_settings
from pal_chat_server.db import get_engine, reset_db_state
from pal_chat_server.models import (
    ChatRun,
    ContextSnapshot,
    Conversation,
    Message,
    Participant,
    ParticipantKind,
    ResponseDecision,
    RunStatus,
    Topic,
)
from pal_chat_server.services import ensure_primary_topic_link, utc_now
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ROOT_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def session(tmp_path: Path) -> Generator[Session, None, None]:
    data_dir = tmp_path / "data"
    db_path = data_dir / "pal-chat.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["PAL_CHAT_DATA_DIR"] = str(data_dir)
    os.environ["PAL_CHAT_DATABASE_URL"] = f"sqlite:///{db_path}"
    get_settings.cache_clear()
    reset_db_state()

    config = Config(str(ROOT_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")

    engine = get_engine()
    with Session(engine) as db:
        yield db
        db.rollback()

    os.environ.pop("PAL_CHAT_DATA_DIR", None)
    os.environ.pop("PAL_CHAT_DATABASE_URL", None)
    get_settings.cache_clear()
    reset_db_state()


def seed_conversation(db: Session) -> tuple[Conversation, Participant, Participant]:
    now = utc_now()
    conversation = Conversation(
        id=str(uuid4()),
        title="T",
        active_topic_id=None,
        created_at=now,
        updated_at=now,
    )
    user = Participant(
        id=str(uuid4()),
        conversation_id=conversation.id,
        kind=ParticipantKind.USER.value,
        display_name="User",
        role_prompt=None,
        decision_threshold=None,
        sort_order=0,
    )
    agent = Participant(
        id=str(uuid4()),
        conversation_id=conversation.id,
        kind=ParticipantKind.AGENT.value,
        display_name="Agent",
        role_prompt="role",
        decision_threshold=0.62,
        sort_order=1,
    )
    db.add_all([conversation, user, agent])
    db.commit()
    return conversation, user, agent


def test_single_active_topic_constraint(session: Session) -> None:
    conversation, user, _ = seed_conversation(session)
    message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        run_id=None,
        author_participant_id=user.id,
        parent_message_id=None,
        caused_by_decision_id=None,
        kind="user",
        content="hi",
        sequence_no=1,
        created_at=utc_now(),
    )
    session.add(message)
    session.commit()

    session.add_all(
        [
            Topic(
                id=str(uuid4()),
                conversation_id=conversation.id,
                title="A",
                status="active",
                interrupted_topic_id=None,
                current_summary_revision_id=None,
                created_by_message_id=message.id,
                created_at=utc_now(),
                updated_at=utc_now(),
            ),
            Topic(
                id=str(uuid4()),
                conversation_id=conversation.id,
                title="B",
                status="active",
                interrupted_topic_id=None,
                current_summary_revision_id=None,
                created_by_message_id=message.id,
                created_at=utc_now(),
                updated_at=utc_now(),
            ),
        ]
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_agent_message_requires_selected_decision(session: Session) -> None:
    conversation, user, agent = seed_conversation(session)
    run = ChatRun(
        id=str(uuid4()),
        conversation_id=conversation.id,
        root_user_message_id=None,
        status=RunStatus.QUEUED.value,
        stop_reason=None,
        max_agent_messages=5,
        max_rounds=6,
        max_total_tokens=30000,
        timeout_ms=90000,
        agent_message_count=0,
        round_count=0,
        reserved_tokens=0,
        actual_tokens=0,
        cancel_generation=0,
        started_at=utc_now(),
        ended_at=None,
    )
    user_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        run_id=run.id,
        author_participant_id=user.id,
        parent_message_id=None,
        caused_by_decision_id=None,
        kind="user",
        content="hello",
        sequence_no=1,
        created_at=utc_now(),
    )
    session.add_all([run, user_message])
    session.commit()

    snapshot = ContextSnapshot(
        id=str(uuid4()),
        run_id=run.id,
        trigger_message_id=user_message.id,
        agent_id=agent.id,
        purpose="decision",
        policy_version="v1",
        tokenizer="test",
        estimated_tokens=1,
        canonical_content_hash="hash",
        created_at=utc_now(),
    )
    decision = ResponseDecision(
        id=str(uuid4()),
        run_id=run.id,
        trigger_message_id=user_message.id,
        agent_id=agent.id,
        snapshot_id=snapshot.id,
        model_call_id=None,
        eligible=True,
        should_reply=True,
        score=0.9,
        threshold=0.62,
        dimensions_json={},
        reason_codes_json=[],
        reply_intent="answer",
        outcome="silent",
        policy_version="v1",
        created_at=utc_now(),
    )
    session.add_all([snapshot, decision])
    session.commit()

    session.add(
        Message(
            id=str(uuid4()),
            conversation_id=conversation.id,
            run_id=run.id,
            author_participant_id=agent.id,
            parent_message_id=user_message.id,
            caused_by_decision_id=decision.id,
            kind="agent",
            content="reply",
            sequence_no=2,
            created_at=utc_now(),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()


def test_terminal_run_state_cannot_reopen(session: Session) -> None:
    conversation, _, _ = seed_conversation(session)
    run = ChatRun(
        id=str(uuid4()),
        conversation_id=conversation.id,
        root_user_message_id=None,
        status=RunStatus.COMPLETED.value,
        stop_reason="no_candidate",
        max_agent_messages=5,
        max_rounds=6,
        max_total_tokens=30000,
        timeout_ms=90000,
        agent_message_count=0,
        round_count=0,
        reserved_tokens=0,
        actual_tokens=0,
        cancel_generation=0,
        started_at=utc_now(),
        ended_at=utc_now(),
    )
    session.add(run)
    session.commit()

    run.status = RunStatus.RUNNING.value
    with pytest.raises(IntegrityError):
        session.commit()


def test_primary_topic_link_helper_rejects_duplicate(session: Session) -> None:
    conversation, user, _ = seed_conversation(session)
    message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        run_id=None,
        author_participant_id=user.id,
        parent_message_id=None,
        caused_by_decision_id=None,
        kind="user",
        content="topic",
        sequence_no=1,
        created_at=utc_now(),
    )
    topic = Topic(
        id=str(uuid4()),
        conversation_id=conversation.id,
        title="A",
        status="inactive",
        interrupted_topic_id=None,
        current_summary_revision_id=None,
        created_by_message_id=message.id,
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    session.add_all([message, topic])
    session.commit()

    ensure_primary_topic_link(session, message_id=message.id, topic_id=topic.id)
    session.commit()

    from pal_chat_server.errors import AppError

    with pytest.raises(AppError):
        ensure_primary_topic_link(session, message_id=message.id, topic_id=topic.id)


def test_sqlite_pragmas_apply_to_every_connection(session: Session) -> None:
    engine = get_engine()
    with engine.connect() as first, engine.connect() as second:
        first_fk = first.execute(text("PRAGMA foreign_keys")).scalar_one()
        second_fk = second.execute(text("PRAGMA foreign_keys")).scalar_one()
        busy_timeout = second.execute(text("PRAGMA busy_timeout")).scalar_one()

    assert first_fk == 1
    assert second_fk == 1
    assert busy_timeout == 5000


def test_active_run_unique_constraint(session: Session) -> None:
    conversation, _, _ = seed_conversation(session)
    first = ChatRun(
        id=str(uuid4()),
        conversation_id=conversation.id,
        root_user_message_id=None,
        status=RunStatus.QUEUED.value,
        stop_reason=None,
        max_agent_messages=5,
        max_rounds=6,
        max_total_tokens=30000,
        timeout_ms=90000,
        agent_message_count=0,
        round_count=0,
        reserved_tokens=0,
        actual_tokens=0,
        cancel_generation=0,
        started_at=utc_now(),
        ended_at=None,
    )
    second = ChatRun(
        id=str(uuid4()),
        conversation_id=conversation.id,
        root_user_message_id=None,
        status=RunStatus.RUNNING.value,
        stop_reason=None,
        max_agent_messages=5,
        max_rounds=6,
        max_total_tokens=30000,
        timeout_ms=90000,
        agent_message_count=0,
        round_count=0,
        reserved_tokens=0,
        actual_tokens=0,
        cancel_generation=0,
        started_at=utc_now(),
        ended_at=None,
    )
    session.add(first)
    session.commit()

    session.add(second)
    with pytest.raises(IntegrityError):
        session.commit()


def test_agent_message_requires_existing_selected_decision(session: Session) -> None:
    conversation, user, agent = seed_conversation(session)
    run = ChatRun(
        id=str(uuid4()),
        conversation_id=conversation.id,
        root_user_message_id=None,
        status=RunStatus.QUEUED.value,
        stop_reason=None,
        max_agent_messages=5,
        max_rounds=6,
        max_total_tokens=30000,
        timeout_ms=90000,
        agent_message_count=0,
        round_count=0,
        reserved_tokens=0,
        actual_tokens=0,
        cancel_generation=0,
        started_at=utc_now(),
        ended_at=None,
    )
    user_message = Message(
        id=str(uuid4()),
        conversation_id=conversation.id,
        run_id=run.id,
        author_participant_id=user.id,
        parent_message_id=None,
        caused_by_decision_id=None,
        kind="user",
        content="hello",
        sequence_no=1,
        created_at=utc_now(),
    )
    session.add_all([run, user_message])
    session.commit()

    session.add(
        Message(
            id=str(uuid4()),
            conversation_id=conversation.id,
            run_id=run.id,
            author_participant_id=agent.id,
            parent_message_id=user_message.id,
            caused_by_decision_id=str(uuid4()),
            kind="agent",
            content="reply",
            sequence_no=2,
            created_at=utc_now(),
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
