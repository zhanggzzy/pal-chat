from __future__ import annotations

import json
from pathlib import Path

import conftest as test_support
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pal_chat_server.app import create_app
from pal_chat_server.db import catalog_path, reset_db_state


def test_manifest_omits_secret_but_keeps_masked_ref(
    migrated_app: TestClient,
    data_dir: Path,
) -> None:
    credential = migrated_app.post(
        "/api/v1/credentials",
        json={"provider": "openai-compatible", "label": "local", "secret": "super-secret-value"},
    ).json()["credential"]
    conversation = migrated_app.post(
        "/api/v1/conversations",
        json={
            "title": "Credential Demo",
            "draft_profile": {
                "schema_version": 1,
                "template_id": "custom-openai",
                "title": "Credential Demo",
                "modules": {
                    "message_sequence": {"module_id": "sequence.sqlite-append-only", "config": {}},
                    "projection": {"module_id": "projection.segment-chain", "config": {}},
                    "trigger": {"module_id": "trigger.debounced-observation", "config": {}},
                    "context_assembly": {"module_id": "context.simple", "config": {}},
                    "memory": {"module_id": "memory.graph-overlay", "config": {}},
                    "decision": {"module_id": "decision.score-threshold", "config": {}},
                    "reconsideration": {"module_id": "reconsideration.rules-first", "config": {}},
                    "timing": {"module_id": "timing.humanized", "config": {}},
                    "delivery": {"module_id": "delivery.broadcast-all", "config": {}},
                    "budget": {"module_id": "budget.causal-episode-4-2-2", "config": {}},
                    "model_adapter": {
                        "module_id": "model.openai-compatible",
                        "config": {"base_url": "https://example.invalid/v1"},
                    },
                },
                "agent_a": {
                    "agent_id": "agent-a",
                    "display_name": "Agent A",
                    "persona_prompt": "A",
                    "attention_prior": "A",
                    "response_threshold": 0.5,
                    "model": {
                        "provider": "openai-compatible",
                        "model": "gpt-test",
                        "credential_ref": credential["credential_ref"],
                        "options": {},
                    },
                },
                "agent_b": {
                    "agent_id": "agent-b",
                    "display_name": "Agent B",
                    "persona_prompt": "B",
                    "attention_prior": "B",
                    "response_threshold": 0.5,
                    "model": {
                        "provider": "openai-compatible",
                        "model": "gpt-test",
                        "credential_ref": credential["credential_ref"],
                        "options": {},
                    },
                },
                "prompt_version": "phase1",
                "metadata": {},
            },
        },
    ).json()
    conversation_id = conversation["id"]

    validation = migrated_app.post(f"/api/v1/conversations/{conversation_id}/validate")
    assert validation.json()["ok"] is True
    started = migrated_app.post(f"/api/v1/conversations/{conversation_id}/start")
    assert started.status_code == 200

    manifest_path = Path(started.json()["manifest_path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    serialized = json.dumps(manifest, ensure_ascii=False)
    assert "super-secret-value" not in serialized
    assert credential["masked_value"] in serialized

    observations = (manifest_path.parent / "observations.ndjson").read_text(encoding="utf-8")
    assert "super-secret-value" not in observations


def test_catalog_rebuild_from_manifest(data_dir: Path) -> None:
    db_path = test_support.configure_test_env(data_dir)

    config = Config(str(test_support.ROOT_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(config, "head")

    first_app = create_app()
    with TestClient(first_app) as client:
        created = client.post("/api/v1/conversations", json={"title": "Rebuild Me"}).json()
        conversation_id = created["id"]
        assert client.post(f"/api/v1/conversations/{conversation_id}/start").status_code == 200
        assert client.post(f"/api/v1/conversations/{conversation_id}/end").status_code == 200

    catalog = catalog_path()
    if catalog.exists():
        catalog.unlink()
    reset_db_state()

    second_app = create_app()
    with TestClient(second_app) as client:
        listing = client.get("/api/v1/conversations")
        assert listing.status_code == 200
        items = listing.json()["items"]
        assert len(items) == 1
        assert items[0]["id"] == conversation_id
        assert items[0]["status"] == "ended"

    test_support.clear_test_env()
