from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.services import health


class _Scheduler:
    running = True

    @staticmethod
    def get_jobs() -> list[object]:
        return [object(), object()]


def test_readiness_reports_local_dependency_status(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        health,
        "audit_provider_routes",
        lambda: {"status": "ok", "mootdx_only": True},
    )
    monkeypatch.setattr(
        health,
        "release_health",
        lambda _data_dir: {"status": "ok", "release_id": "release-1"},
    )
    monkeypatch.setattr(
        health,
        "_check_data_freshness",
        lambda _data_dir: {"status": "ok"},
    )
    monkeypatch.setattr(
        health,
        "_check_task_state",
        lambda: {"status": "ok"},
    )
    app_state = SimpleNamespace(repo=object(), scheduler=_Scheduler())

    result = health.readiness(app_state, tmp_path)

    assert result["ready"] is True
    assert result["status"] == "ok"
    assert result["checks"]["scheduler"] == {
        "status": "ok",
        "running": True,
        "jobs": 2,
    }


def test_readiness_fails_when_data_directory_is_not_writable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(health.os, "access", lambda _path, _mode: False)
    monkeypatch.setattr(
        health,
        "audit_provider_routes",
        lambda: {"status": "ok"},
    )
    monkeypatch.setattr(
        health,
        "release_health",
        lambda _data_dir: {"status": "ok"},
    )
    monkeypatch.setattr(
        health,
        "_check_data_freshness",
        lambda _data_dir: {"status": "ok"},
    )
    monkeypatch.setattr(
        health,
        "_check_task_state",
        lambda: {"status": "ok"},
    )

    result = health.readiness(
        SimpleNamespace(repo=object(), scheduler=_Scheduler()),
        tmp_path,
    )

    assert result["ready"] is False
    assert result["status"] == "error"
    assert result["checks"]["data_dir"]["writable"] is False


def test_health_routes_keep_liveness_shallow_and_readiness_strict(
    tmp_path,
    monkeypatch,
) -> None:
    app = FastAPI()
    app.include_router(routes.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(
        health,
        "readiness",
        lambda _state, _data_dir: {
            "status": "error",
            "ready": False,
            "checked_at": "2026-10-07T00:00:00Z",
            "checks": {"scheduler": {"status": "error"}},
        },
    )
    client = TestClient(app)

    assert client.get("/health/live").status_code == 200
    assert client.get("/health/live").json()["status"] == "ok"
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["ready"] is False
    assert client.get("/api/health").status_code == 503
