from __future__ import annotations

from fastapi.testclient import TestClient
from pal_chat_server.db import get_engine
from pal_chat_server.models import (
    AgentTopicIndex,
    ContextSnapshot,
    ContextSnapshotItem,
    Conversation,
    Message,
    MessageTopicLink,
    ModelCall,
    Participant,
    ResponseDecision,
    Topic,
    TopicSummaryRevision,
    TopicTransition,
)
from pal_chat_server.services import utc_now
from sqlalchemy.orm import Session


def create_conversation(client: TestClient) -> str:
    response = client.post("/api/v1/conversations", json={"title": "Demo"})
    assert response.status_code == 201
    return str(response.json()["conversation"]["id"])


def test_create_conversation_and_send_message(migrated_app: TestClient) -> None:
    conversation_id = create_conversation(migrated_app)

    response = migrated_app.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "msg-1"},
        json={"content": "Hello team"},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"

    detail = migrated_app.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert detail.json()["active_run"]["id"] == payload["run_id"]

    repeat = migrated_app.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "msg-1"},
        json={"content": "Hello team"},
    )
    assert repeat.status_code == 202
    assert repeat.json() == payload


def test_list_conversations_returns_recent_summaries(migrated_app: TestClient) -> None:
    first_id = create_conversation(migrated_app)
    second_id = create_conversation(migrated_app)
    migrated_app.post(
        f"/api/v1/conversations/{second_id}/messages",
        headers={"Idempotency-Key": "msg-1"},
        json={"content": "Latest preview"},
    )

    response = migrated_app.get("/api/v1/conversations")
    assert response.status_code == 200
    payload = response.json()["items"]
    assert [item["id"] for item in payload] == [second_id, first_id]
    assert payload[0]["message_count"] == 1
    assert payload[0]["active_run_status"] == "queued"
    assert payload[0]["last_message_preview"] == "Latest preview"


def test_send_message_while_run_in_progress_returns_conflict(migrated_app: TestClient) -> None:
    conversation_id = create_conversation(migrated_app)
    first = migrated_app.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "msg-1"},
        json={"content": "First"},
    )
    assert first.status_code == 202

    second = migrated_app.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "msg-2"},
        json={"content": "Second"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "run_in_progress"


def test_stop_run_is_idempotent(migrated_app: TestClient) -> None:
    conversation_id = create_conversation(migrated_app)
    created = migrated_app.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "msg-1"},
        json={"content": "Stop later"},
    ).json()

    stopped = migrated_app.post(
        f"/api/v1/runs/{created['run_id']}/stop",
        headers={"Idempotency-Key": "stop-1"},
    )
    assert stopped.status_code == 202
    assert stopped.json()["status"] == "stop_requested"

    repeated = migrated_app.post(
        f"/api/v1/runs/{created['run_id']}/stop",
        headers={"Idempotency-Key": "stop-1"},
    )
    assert repeated.status_code == 202
    assert repeated.json() == stopped.json()


def test_sse_replays_committed_events(migrated_app: TestClient) -> None:
    conversation_id = create_conversation(migrated_app)
    migrated_app.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "msg-1"},
        json={"content": "Stream me"},
    )

    with migrated_app.stream("GET", f"/api/v1/conversations/{conversation_id}/events") as response:
        body = b"".join(response.iter_bytes())
    assert response.status_code == 200
    assert b"event: message.created" in body


def test_topic_and_analysis_queries_serialize_typed_records(migrated_app: TestClient) -> None:
    conversation_id = create_conversation(migrated_app)
    created = migrated_app.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "msg-1"},
        json={"content": "Analyze me"},
    ).json()

    with Session(get_engine()) as session:
        conversation = session.get(Conversation, conversation_id)
        assert conversation is not None

        message = session.get(Message, created["message_id"])
        assert message is not None

        run_id = created["run_id"]
        participants = list(
            session.query(Participant)
            .filter(Participant.conversation_id == conversation_id)
            .order_by(Participant.sort_order.asc())
        )
        agent = participants[1]
        now = utc_now()
        topic = Topic(
            id="topic-1",
            conversation_id=conversation_id,
            title="Topic A",
            status="active",
            interrupted_topic_id=None,
            current_summary_revision_id=None,
            created_by_message_id=message.id,
            created_at=now,
            updated_at=now,
        )
        session.add(topic)
        session.flush()
        conversation.active_topic_id = topic.id

        model_call = ModelCall(
            id="call-1",
            run_id=run_id,
            purpose="decision",
            agent_id=agent.id,
            provider="fake",
            model="fake-model",
            provider_request_id="req-1",
            status="completed",
            input_tokens=10,
            output_tokens=5,
            cached_tokens=0,
            token_count_source="provider",
            latency_ms=12,
            pricing_version=None,
            estimated_cost_micros=None,
            error_category=None,
            started_at=now,
            ended_at=now,
        )
        session.add(model_call)
        session.flush()

        summary = TopicSummaryRevision(
            id="summary-1",
            topic_id=topic.id,
            content="summary",
            through_message_id=message.id,
            model_call_id=model_call.id,
            estimated_tokens=12,
            created_at=now,
        )
        session.add(summary)
        session.flush()
        topic.current_summary_revision_id = summary.id

        transition = TopicTransition(
            id="transition-1",
            conversation_id=conversation_id,
            from_topic_id=None,
            to_topic_id=topic.id,
            cause_message_id=message.id,
            action="create",
            previous_status_json={},
            created_at=now,
        )
        session.add(transition)

        snapshot = ContextSnapshot(
            id="snapshot-1",
            run_id=run_id,
            trigger_message_id=message.id,
            agent_id=agent.id,
            purpose="decision",
            policy_version="v1",
            tokenizer="test",
            estimated_tokens=20,
            canonical_content_hash="hash",
            created_at=now,
        )
        session.add(snapshot)
        session.flush()

        snapshot_item = ContextSnapshotItem(
            id="snapshot-item-1",
            snapshot_id=snapshot.id,
            ordinal=1,
            item_type="message",
            message_id=message.id,
            summary_revision_id=None,
            rendered_content="Analyze me",
            estimated_tokens=4,
            inclusion_reason="trigger_message",
        )
        session.add(snapshot_item)

        decision = ResponseDecision(
            id="decision-1",
            run_id=run_id,
            trigger_message_id=message.id,
            agent_id=agent.id,
            snapshot_id=snapshot.id,
            model_call_id=model_call.id,
            eligible=True,
            should_reply=True,
            score=0.8,
            threshold=0.62,
            dimensions_json={"relevance": 0.8},
            reason_codes_json=["ROLE_EXPERTISE"],
            reply_intent="answer",
            outcome="selected",
            policy_version="v1",
            created_at=now,
        )
        session.add(decision)

        link = MessageTopicLink(
            id="link-1",
            message_id=message.id,
            topic_id=topic.id,
            kind="primary",
            confidence=1.0,
            route_action="create",
            route_method="rule",
            signals_json=["explicit_callback"],
            policy_version="v1",
            is_override=False,
            created_at=now,
        )
        session.add(link)

        index = AgentTopicIndex(
            agent_id=agent.id,
            topic_id=topic.id,
            salience=0.7,
            role_affinity=0.9,
            familiarity=0.4,
            last_seen_message_id=message.id,
            last_selected_at=now,
            reason_codes_json=["ROLE_EXPERTISE"],
            index_version="v1",
            updated_at=now,
        )
        session.add(index)
        session.commit()

    topic_detail = migrated_app.get("/api/v1/topics/topic-1")
    assert topic_detail.status_code == 200
    assert topic_detail.json()["transitions"][0]["action"] == "create"
    assert topic_detail.json()["summaries"][0]["content"] == "summary"

    analysis = migrated_app.get(f"/api/v1/messages/{created['message_id']}/analysis")
    assert analysis.status_code == 200
    assert analysis.json()["topic_links"][0]["kind"] == "primary"
    assert analysis.json()["snapshot_items"][0]["item_type"] == "message"
    assert analysis.json()["decisions"][0]["outcome"] == "selected"
    assert analysis.json()["model_calls"][0]["provider"] == "fake"

    topics = migrated_app.get(f"/api/v1/conversations/{conversation_id}/topics")
    assert topics.status_code == 200
    assert topics.json()["items"][0]["message_count"] == 1
    assert topics.json()["items"][0]["summary_count"] == 1
