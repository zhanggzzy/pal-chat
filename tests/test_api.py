from __future__ import annotations

from fastapi.testclient import TestClient


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
