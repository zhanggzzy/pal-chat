from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient


def create_conversation(client: TestClient) -> dict[str, object]:
    response = client.post("/api/v1/conversations", json={"title": "Phase 1 Demo"})
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def test_bootstrap_returns_registry_and_default_template(migrated_app: TestClient) -> None:
    response = migrated_app.get("/api/v1/bootstrap")
    assert response.status_code == 200
    payload = response.json()
    assert payload["app_name"] == "pal-chat-server"
    assert payload["templates"][0]["id"] == "default-natural-chat-v1"
    assert any(item["module_id"] == "model.scripted" for item in payload["module_registry"])
    assert payload["note"] == "Issue baseline document header lists update date 2026-07-30."


def test_conversation_lifecycle_and_profile_lock(migrated_app: TestClient) -> None:
    created = create_conversation(migrated_app)
    conversation_id = created["id"]

    validate = migrated_app.post(f"/api/v1/conversations/{conversation_id}/validate")
    assert validate.status_code == 200
    assert validate.json()["ok"] is True

    started = migrated_app.post(f"/api/v1/conversations/{conversation_id}/start")
    assert started.status_code == 200
    started_payload = started.json()
    assert started_payload["status"] == "running"
    assert started_payload["profile_hash"]
    assert started_payload["locked_profile"]["title"] == "Default Natural Chat v1"

    update = migrated_app.put(
        f"/api/v1/conversations/{conversation_id}/draft-profile",
        json={"draft_profile": started_payload["draft_profile"]},
    )
    assert update.status_code == 409
    assert update.json()["error"]["code"] == "profile_locked"

    ended = migrated_app.post(f"/api/v1/conversations/{conversation_id}/end")
    assert ended.status_code == 200
    assert ended.json()["status"] == "ended"

    detail = migrated_app.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert detail.json()["manifest"]["status"] == "ended"
