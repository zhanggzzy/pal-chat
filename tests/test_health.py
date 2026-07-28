from __future__ import annotations

from fastapi.testclient import TestClient


def test_live_health(migrated_app: TestClient) -> None:
    response = migrated_app.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "live", "schema_revision": None}


def test_ready_health(migrated_app: TestClient) -> None:
    response = migrated_app.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["schema_revision"] == "20260728_0001"
