from __future__ import annotations

import json

from app.backtest.strategy import _strategy_definition_hash
from app.enriched_generation import (
    bump_enriched_generation,
    get_enriched_generation,
)
from app.services import data_release, provider_audit
from app.strategy.engine import StrategyEngine


def _audit() -> dict:
    routes = {
        capability["id"]: {"provider": "mootdx", "usable": True}
        for capability in provider_audit.CAPABILITY_REGISTRY
    }
    return {
        "status": "ok",
        "healthy": True,
        "policy": "mootdx_only",
        "single_source": True,
        "mootdx_only": True,
        "mixed_sources": False,
        "sources": ["mootdx"],
        "routes": routes,
        "unavailable": [],
        "issues": [],
    }


def test_release_manifest_is_immutable_and_backtest_traceable(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(data_release, "audit_provider_routes", _audit)
    generation = get_enriched_generation(tmp_path, "stock")
    partition = tmp_path / "kline_daily_enriched" / "date=2026-10-07"
    partition.mkdir(parents=True)
    (partition / "part.parquet").write_bytes(b"sample")

    first = data_release.publish_data_release(tmp_path, reason="daily_pipeline")
    second = data_release.publish_data_release(tmp_path, reason="financial_sync:all")

    assert first["release_id"] != second["release_id"]
    assert first["datasets"]["stock_enriched"]["generation"] == generation
    assert first["datasets"]["stock_enriched"]["latest_partition"] == "2026-10-07"
    assert data_release.read_current_release(tmp_path)["release_id"] == second["release_id"]
    assert (
        tmp_path
        / "data_releases"
        / "manifests"
        / f"{first['release_id']}.json"
    ).exists()

    provenance = data_release.backtest_provenance(
        tmp_path,
        asset_type="stock",
        config={"matching": "open_t+1", "fees_pct": 0.0002},
        strategy_hash="strategy-sha256",
        data_generation=generation,
    )
    assert provenance["dataset_release_id"] == second["release_id"]
    assert provenance["release_consistent"] is True
    assert provenance["strategy_hash"] == "strategy-sha256"
    assert len(provenance["config_hash"]) == 64


def test_release_health_reports_unpublished_generation_change(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(data_release, "audit_provider_routes", _audit)
    get_enriched_generation(tmp_path, "stock")
    release = data_release.publish_data_release(tmp_path, reason="baseline")

    assert data_release.release_health(tmp_path)["status"] == "ok"
    bump_enriched_generation(tmp_path, "stock")

    health = data_release.release_health(tmp_path)
    assert health["release_id"] == release["release_id"]
    assert health["status"] == "warning"
    assert health["generation_mismatches"] == ["stock_enriched"]


def test_invalid_current_manifest_fails_closed(tmp_path) -> None:
    path = tmp_path / "data_releases" / "current.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    health = data_release.release_health(tmp_path)
    assert health["status"] == "error"
    assert health["release_id"] is None


def test_backtest_manifest_persists_compact_trace(tmp_path) -> None:
    manifest = data_release.persist_backtest_manifest(
        tmp_path,
        kind="strategy",
        result={
            "run_id": "run-1",
            "config": {"strategy_id": "traceable", "matching": "open_t+1"},
            "provenance": {
                "dataset_release_id": "release-1",
                "strategy_hash": "source-hash",
            },
            "stats": {"total_return": 0.12},
            "trades": [{"symbol": "600000.SH"}],
            "equity_curve": [{"date": "2026-10-07", "value": 1.12}],
        },
    )

    saved = json.loads(
        (
            tmp_path
            / "backtest_results"
            / "manifests"
            / "run-1.json"
        ).read_text(encoding="utf-8")
    )
    assert saved == manifest
    assert saved["trade_count"] == 1
    assert saved["stats_hash"]
    assert "equity_curve" not in saved


def _matrix(routes: dict[str, str], unusable: set[str] | None = None) -> dict:
    unusable = unusable or set()
    return {
        "tickflow_tier": "none",
        "capabilities": [
            {
                "id": capability["id"],
                "effective": routes[capability["field"]],
                "usable": capability["id"] not in unusable,
            }
            for capability in provider_audit.CAPABILITY_REGISTRY
        ],
    }


def test_provider_audit_distinguishes_mootdx_only_and_mixed(
    monkeypatch,
) -> None:
    all_mootdx = {
        capability["field"]: "mootdx"
        for capability in provider_audit.CAPABILITY_REGISTRY
    }
    monkeypatch.setattr(
        provider_audit,
        "current_provider_routes",
        lambda: dict(all_mootdx),
    )
    monkeypatch.setattr(
        provider_audit,
        "build_capability_matrix",
        lambda current, tickflow_tier: _matrix(current),
    )

    audit = provider_audit.audit_provider_routes(tickflow_tier="none")
    assert audit["status"] == "ok"
    assert audit["mootdx_only"] is True

    mixed = dict(all_mootdx)
    mixed["financial_data_provider"] = "tickflow"
    monkeypatch.setattr(
        provider_audit,
        "current_provider_routes",
        lambda: mixed,
    )
    audit = provider_audit.audit_provider_routes(tickflow_tier="none")
    assert audit["status"] == "warning"
    assert audit["policy"] == "mixed_with_mootdx"
    assert audit["issues"][0]["code"] == "mootdx_mixed_sources"


def test_strategy_hash_changes_with_source_content(tmp_path) -> None:
    path = tmp_path / "traceable.py"
    path.write_text(
        """import polars as pl
META = {
    "id": "traceable",
    "name": "traceable",
    "asset_types": ["stock"],
    "timeframes": ["1d"],
}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.lit(True)
""",
        encoding="utf-8",
    )
    engine = StrategyEngine(strategy_dirs=[tmp_path])
    first = _strategy_definition_hash(engine.get("traceable"), engine)

    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "return pl.lit(True)",
            "return pl.lit(False)",
        ),
        encoding="utf-8",
    )
    second = _strategy_definition_hash(engine.get("traceable"), engine)

    assert first != second
