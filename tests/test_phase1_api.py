from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient
from pal_chat_server.registry import MODULE_REGISTRY, ModuleDescriptor


def create_conversation(client: TestClient) -> dict[str, object]:
    response = client.post("/api/v1/conversations", json={"title": "Phase 1 Demo"})
    assert response.status_code == 201
    return cast(dict[str, object], response.json())


def default_profile_payload(client: TestClient) -> dict[str, object]:
    response = client.post("/api/v1/profile-templates/default-natural-chat-v1/clone")
    assert response.status_code == 200
    return cast(dict[str, object], response.json()["profile"])


def test_bootstrap_returns_registry_and_default_template(migrated_app: TestClient) -> None:
    response = migrated_app.get("/api/v1/bootstrap")
    assert response.status_code == 200
    payload = response.json()
    assert payload["app_name"] == "pal-chat-server"
    assert payload["templates"][0]["id"] == "default-natural-chat-v1"
    assert any(item["module_id"] == "model.scripted" for item in payload["module_registry"])
    assert payload["note"] is None


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


def test_start_rejects_running_and_ended(migrated_app: TestClient) -> None:
    created = create_conversation(migrated_app)
    conversation_id = created["id"]

    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/start").status_code == 200

    running_start = migrated_app.post(f"/api/v1/conversations/{conversation_id}/start")
    assert running_start.status_code == 409
    assert running_start.json()["error"]["code"] == "invalid_transition"

    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/end").status_code == 200

    ended_start = migrated_app.post(f"/api/v1/conversations/{conversation_id}/start")
    assert ended_start.status_code == 409
    assert ended_start.json()["error"]["code"] == "invalid_transition"

    detail = migrated_app.get(f"/api/v1/conversations/{conversation_id}")
    assert detail.status_code == 200
    assert detail.json()["conversation"]["status"] == "ended"
    assert detail.json()["manifest"]["status"] == "ended"


def test_pause_and_resume_reject_invalid_transitions(migrated_app: TestClient) -> None:
    created = create_conversation(migrated_app)
    conversation_id = created["id"]

    pause_before_start = migrated_app.post(f"/api/v1/conversations/{conversation_id}/pause")
    assert pause_before_start.status_code == 409
    assert pause_before_start.json()["error"]["code"] == "invalid_transition"

    resume_before_start = migrated_app.post(f"/api/v1/conversations/{conversation_id}/resume")
    assert resume_before_start.status_code == 409
    assert resume_before_start.json()["error"]["code"] == "invalid_transition"

    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/start").status_code == 200
    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/pause").status_code == 200

    duplicate_pause = migrated_app.post(f"/api/v1/conversations/{conversation_id}/pause")
    assert duplicate_pause.status_code == 409
    assert duplicate_pause.json()["error"]["code"] == "invalid_transition"

    assert migrated_app.post(f"/api/v1/conversations/{conversation_id}/resume").status_code == 200

    duplicate_resume = migrated_app.post(f"/api/v1/conversations/{conversation_id}/resume")
    assert duplicate_resume.status_code == 409
    assert duplicate_resume.json()["error"]["code"] == "invalid_transition"


def test_validate_rejects_conflicting_modules_by_selected_module_id(
    migrated_app: TestClient,
) -> None:
    profile = default_profile_payload(migrated_app)

    original = next(item for item in MODULE_REGISTRY if item.module_id == "model.scripted")
    original_conflicts = list(original.conflicts_with)
    original.conflicts_with = ["projection.segment-chain"]
    try:
        created = migrated_app.post(
            "/api/v1/conversations",
            json={"title": "Conflict Demo", "draft_profile": profile},
        ).json()
        conversation_id = created["id"]

        validate = migrated_app.post(f"/api/v1/conversations/{conversation_id}/validate")
        assert validate.status_code == 200
        payload = validate.json()
        assert payload["ok"] is False
        assert any(issue["code"] == "conflicting_module" for issue in payload["issues"])

        start = migrated_app.post(f"/api/v1/conversations/{conversation_id}/start")
        assert start.status_code == 409
        assert start.json()["error"]["code"] == "profile_invalid"
    finally:
        original.conflicts_with = original_conflicts


def test_validate_rejects_missing_capability_combo(migrated_app: TestClient) -> None:
    profile = default_profile_payload(migrated_app)
    modules = cast(dict[str, dict[str, object]], profile["modules"])
    original_memory = modules["memory"]

    temp_module = ModuleDescriptor(
        module_id="memory.no-graph",
        implementation_version="1.0.0",
        state_schema_version=1,
        protocol_family="memory-v1",
        kind="memory",
        provides_capabilities=[],
        supports_recovery=True,
        visualization_kinds=["raw-json"],
    )
    MODULE_REGISTRY.append(temp_module)
    modules["memory"] = {"module_id": "memory.no-graph", "config": {}}
    try:
        created = migrated_app.post(
            "/api/v1/conversations",
            json={"title": "Missing Capability Demo", "draft_profile": profile},
        ).json()
        conversation_id = created["id"]

        validate = migrated_app.post(f"/api/v1/conversations/{conversation_id}/validate")
        assert validate.status_code == 200
        payload = validate.json()
        assert payload["ok"] is False
        assert any(issue["code"] == "missing_capability" for issue in payload["issues"])

        start = migrated_app.post(f"/api/v1/conversations/{conversation_id}/start")
        assert start.status_code == 409
        assert start.json()["error"]["code"] == "profile_invalid"
    finally:
        modules["memory"] = original_memory
        MODULE_REGISTRY.pop()
