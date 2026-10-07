"""Computed-cache invalidation must preserve and restore its data dependencies."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl

from app.api import ext_data as ext_data_api
from app.api import signals as signals_api
from app.tickflow.repository import DataStore, KlineRepository


def _repo(tmp_path) -> KlineRepository:
    return KlineRepository(DataStore(tmp_path))


def test_computed_invalidation_preserves_instrument_dependencies(tmp_path):
    repo = _repo(tmp_path)
    stock = pl.DataFrame({"symbol": ["600519.SH"]})
    index = pl.DataFrame({"symbol": ["000001.SH"]})
    etf = pl.DataFrame({"symbol": ["588200.SH"]})
    repo._instruments_cache = stock
    repo._index_instruments_cache = index
    repo._etf_instruments_cache = etf
    repo._enriched_cache = pl.DataFrame({"symbol": ["600519.SH"]})
    repo._enriched_history_cache = pl.DataFrame({"symbol": ["600519.SH"]})
    repo._live_agg_cache = pl.DataFrame({"symbol": ["600519.SH"]})

    repo.invalidate_computed_caches()

    assert repo._instruments_cache is stock
    assert repo._index_instruments_cache is index
    assert repo._etf_instruments_cache is etf
    assert repo._enriched_cache is None
    assert repo._enriched_history_cache is None
    assert repo._live_agg_cache is None


def test_full_clear_still_clears_instrument_dependencies(tmp_path):
    repo = _repo(tmp_path)
    repo._instruments_cache = pl.DataFrame({"symbol": ["600519.SH"]})
    repo._index_instruments_cache = pl.DataFrame({"symbol": ["000001.SH"]})
    repo._etf_instruments_cache = pl.DataFrame({"symbol": ["588200.SH"]})

    repo.clear_cache()

    assert repo._instruments_cache is None
    assert repo._index_instruments_cache is None
    assert repo._etf_instruments_cache is None


def test_lazy_enriched_refresh_loads_instruments_before_compute(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    calls = []

    def load_instruments():
        calls.append("instruments")
        repo._instruments_cache = pl.DataFrame({"symbol": ["600519.SH"]})

    monkeypatch.setattr(repo, "_refresh_instruments", load_instruments)
    monkeypatch.setattr(repo, "get_matrix_data_generation", lambda _asset: "generation")
    monkeypatch.setattr(repo, "_latest_enriched_date_duckdb", lambda: None)

    repo._refresh_enriched_impl()

    assert calls == ["instruments"]


def test_lazy_refresh_after_full_clear_keeps_limit_signals(tmp_path):
    instrument_dir = tmp_path / "instruments"
    instrument_dir.mkdir()
    pl.DataFrame({
        "symbol": ["600000.SH"],
        "name": ["浦发银行"],
        "listing_date": [date(1999, 11, 10)],
        "limit_up": [None],
        "limit_down": [None],
        "as_of": [date(2026, 10, 7)],
    }, schema_overrides={"limit_up": pl.Float64, "limit_down": pl.Float64}).write_parquet(
        instrument_dir / "instruments.parquet"
    )
    rows = [
        {
            "symbol": "600000.SH", "date": date(2026, 9, 29),
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "volume": 1000.0, "amount": 1_000_000.0,
            "raw_close": 10.0, "raw_high": 10.0, "raw_low": 10.0,
        },
        {
            "symbol": "600000.SH", "date": date(2026, 9, 30),
            "open": 10.5, "high": 11.0, "low": 10.5, "close": 11.0,
            "volume": 2000.0, "amount": 2_200_000.0,
            "raw_close": 11.0, "raw_high": 11.0, "raw_low": 10.5,
        },
    ]
    for row in rows:
        target = tmp_path / "kline_daily_enriched" / f"date={row['date']}"
        target.mkdir(parents=True)
        pl.DataFrame([row]).write_parquet(target / "part.parquet")

    repo = _repo(tmp_path)
    repo.clear_cache()
    latest, latest_date = repo.get_enriched_latest()

    assert latest_date == date(2026, 9, 30)
    assert latest["signal_limit_up"].to_list() == [True]
    assert latest["consecutive_limit_ups"].to_list() == [1]


def test_ext_data_invalidation_keeps_dependency_caches(tmp_path):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path, db=MagicMock()),
        invalidate_computed_caches=MagicMock(),
        clear_cache=MagicMock(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))

    ext_data_api._refresh_views(request)

    repo.invalidate_computed_caches.assert_called_once_with()
    repo.clear_cache.assert_not_called()


def test_signal_invalidation_keeps_dependency_caches(tmp_path, monkeypatch):
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        invalidate_computed_caches=MagicMock(),
        clear_cache=MagicMock(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    monkeypatch.setattr(signals_api.custom_signals, "invalidate_intraday_cache", lambda: None)
    monkeypatch.setattr(
        "app.indicators.pipeline.invalidate_custom_signals", lambda: None
    )
    monkeypatch.setattr("app.services.strategy_cache.clear_cache", lambda _path: None)

    signals_api._invalidate(request)

    repo.invalidate_computed_caches.assert_called_once_with()
    repo.clear_cache.assert_not_called()
