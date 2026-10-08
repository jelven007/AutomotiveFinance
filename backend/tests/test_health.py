from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import routes
from app.market_time import CN_TZ
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


@pytest.mark.parametrize("check", [
    "_check_data_dir", "_check_disk", "_check_scheduler", "audit_provider_routes",
    "_check_data_freshness", "_check_task_state", "release_health",
])
def test_check_exception_preserves_readiness_diagnostics(tmp_path, monkeypatch, check):
    def broken(*args):
        raise OSError("injected dependency failure")

    monkeypatch.setattr(health, check, broken)
    result = health.readiness(SimpleNamespace(repo=object(), scheduler=_Scheduler()), tmp_path)
    assert result["ready"] is False
    assert len(result["checks"]) == 8
    assert any(
        item.get("error_type") == "OSError" and item["status"] == "error"
        for item in result["checks"].values()
    )
    assert result["checks"]["repository"]["status"] == "ok"


def _freshness_files(tmp_path, latest="2026-09-30"):
    for directory in ("kline_daily", "kline_daily_enriched", "kline_minute", "kline_index_daily"):
        (tmp_path / directory / f"date={latest}").mkdir(parents=True)


def test_long_holiday_uses_calendar_instead_of_elapsed_days(tmp_path, monkeypatch):
    _freshness_files(tmp_path)
    # 人工交易日历用于验证长假边界, 不代表现实交易所日历。
    monkeypatch.setattr(health, "cached_trading_calendar", lambda: {
        date(2026, 9, 30), date(2026, 10, 12),
    })
    monkeypatch.setattr(health, "cn_now", lambda: datetime(2026, 10, 8, 19, tzinfo=CN_TZ))
    result = health._check_data_freshness(tmp_path)
    assert result["status"] == "ok"
    assert result["calendar_lag_days"] == 8
    assert result["observed_trading_lag"] == 0
    assert result["reference_trading_date"] == "2026-09-30"


def test_one_missing_session_is_detected_after_close_grace(tmp_path, monkeypatch):
    _freshness_files(tmp_path, "2026-09-29")
    monkeypatch.setattr(health, "cached_trading_calendar", lambda: {
        date(2026, 9, 29), date(2026, 9, 30),
    })
    monkeypatch.setattr(health, "cn_now", lambda: datetime(2026, 9, 30, 17, tzinfo=CN_TZ))
    assert health._check_data_freshness(tmp_path)["status"] == "ok"
    monkeypatch.setattr(health, "cn_now", lambda: datetime(2026, 9, 30, 19, tzinfo=CN_TZ))
    result = health._check_data_freshness(tmp_path)
    assert result["observed_trading_lag"] == 1
    assert result["stale_datasets"] == ["stock_daily", "stock_enriched", "stock_minute"]


def test_missing_calendar_is_unknown_and_never_fetches_provider(tmp_path, monkeypatch):
    from app.services import data_integrity

    _freshness_files(tmp_path)
    monkeypatch.setattr(data_integrity, "_CAL", (0.0, None))

    def network_forbidden():
        pytest.fail("health must not fetch a trading calendar")

    monkeypatch.setattr(data_integrity, "_trading_calendar", network_forbidden)
    monkeypatch.setattr(health, "cn_now", lambda: datetime(2026, 10, 8, 19, tzinfo=CN_TZ))
    result = health._check_data_freshness(tmp_path)
    assert result["calendar_coverage"] == "unknown"
    assert result["stale_datasets"] == []
    assert result["status"] == "warning"
