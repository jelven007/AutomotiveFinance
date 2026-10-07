"""Integration of the selected provider with stock/index/ETF persistence."""

from datetime import date, datetime

import polars as pl
import pytest

from app.data_providers import custom as custom_sources
from app.plugins.mootdx.provider import MootdxProvider
from app.services import index_sync, instrument_sync, kline_sync, preferences
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def selected(monkeypatch):
    provider = MootdxProvider()
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "mootdx")
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "mootdx")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: name == "mootdx")
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda name, ds: name == "mootdx")
    for module in (index_sync, instrument_sync, kline_sync):
        monkeypatch.setattr(
            module, "get_client",
            lambda: pytest.fail("An explicitly selected mootdx operation must not call TickFlow"),
        )
    return provider


def test_instrument_failure_does_not_fall_back_or_overwrite(tmp_path, selected, monkeypatch):
    target = tmp_path / "instruments" / "instruments.parquet"
    target.parent.mkdir()
    pl.DataFrame({"symbol": ["600519.SH"], "name": ["贵州茅台"]}).write_parquet(target)
    before = target.read_bytes()

    def fail(_asset_type):
        raise RuntimeError("offline")

    monkeypatch.setattr(selected, "get_instruments", fail)
    assert instrument_sync.sync_instruments(tmp_path) == 0
    assert target.read_bytes() == before


def test_index_and_etf_catalogs_follow_daily_provider(tmp_path, selected, monkeypatch):
    monkeypatch.setattr(
        selected, "get_instruments",
        lambda asset_type: [{
            "symbol": "000001.SH" if asset_type == "index" else "588200.SH",
            "name": "上证指数" if asset_type == "index" else "科创ETF",
        }],
    )
    repo = KlineRepository(DataStore(tmp_path))
    try:
        assert index_sync.sync_index_instruments(repo) == 2
        assert repo.get_index_instruments()["symbol"].to_list() == ["000001.SH"]
        assert repo.get_etf_instruments()["symbol"].to_list() == ["588200.SH"]
    finally:
        repo.db.close()


@pytest.mark.parametrize("asset_type,symbol", [("index", "000001.SH"), ("etf", "588200.SH")])
def test_index_and_etf_daily_persist_selected_provider(
    tmp_path, selected, monkeypatch, asset_type, symbol,
):
    calls = []

    def daily(symbols, start_time, end_time, asset_type="stock", **kwargs):
        calls.append((symbols, asset_type))
        return pl.DataFrame({
            "symbol": [symbol], "date": [date(2026, 9, 30)], "open": [10.0],
            "high": [11.0], "low": [9.0], "close": [10.5], "volume": [1000.0],
            "amount": [1_050_000.0],
        })

    monkeypatch.setattr(selected, "get_daily", daily)
    repo = KlineRepository(DataStore(tmp_path))
    caps = CapabilitySet({})  # Custom sources must work without TickFlow permissions.
    try:
        sync = (
            index_sync.sync_and_persist_index_daily if asset_type == "index"
            else index_sync.sync_and_persist_etf_daily
        )
        assert sync(
            repo, caps, symbols_override=[symbol],
            start_date=datetime(2026, 9, 30), end_date=datetime(2026, 9, 30, 15),
        ) == 1
        assert calls == [([symbol], asset_type)]
        raw = pl.read_parquet(
            tmp_path / f"kline_{asset_type}_daily" / "date=2026-09-30" / "part.parquet"
        )
        assert raw["volume"][0] == 1000
        assert raw["close"][0] == 10.5
    finally:
        repo.db.close()


def test_empty_etf_adjustments_do_not_change_source(tmp_path, selected, monkeypatch):
    monkeypatch.setattr(selected, "get_adj_factors", lambda *a, **k: pl.DataFrame())
    repo = KlineRepository(DataStore(tmp_path))
    try:
        assert kline_sync.sync_adj_factor(
            ["588200.SH"], repo,
            CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=50, rpm=None)}),
            asset_type="etf",
        ) == (0, [])
    finally:
        repo.db.close()


@pytest.mark.parametrize("asset_type", ["stock", "index", "etf"])
def test_unavailable_catalog_keeps_existing_data(tmp_path, selected, monkeypatch, asset_type):
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: False)
    if asset_type == "stock":
        assert instrument_sync.sync_instruments(tmp_path) == 0
    else:
        assert index_sync._fetch_instruments_by_type(asset_type, asset_type).is_empty()


def test_missing_daily_capability_never_uses_tickflow(tmp_path, selected, monkeypatch):
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *a: False)
    repo = KlineRepository(DataStore(tmp_path))
    try:
        zero = []
        assert kline_sync.sync_and_persist_daily_batch(
            ["600519.SH"], repo,
            CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=50, rpm=None)}),
            zero_row_out=zero,
        ) == 0
        assert zero == ["600519.SH"]
        assert kline_sync.sync_adj_factor(
            ["600519.SH"], repo,
            CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=50, rpm=None)}),
        ) == (0, [])
    finally:
        repo.db.close()


@pytest.mark.parametrize("failure", ["missing", "resolve", "fetch", "schema"])
def test_minute_failure_never_uses_tickflow(tmp_path, selected, monkeypatch, failure):
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "mootdx")

    def fail(*args, **kwargs):
        raise RuntimeError("offline")

    if failure == "missing":
        monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *a: False)
    elif failure == "resolve":
        monkeypatch.setattr(custom_sources, "get_provider", fail)
    elif failure == "fetch":
        monkeypatch.setattr(selected, "get_minute", fail)
    else:
        monkeypatch.setattr(
            selected, "get_minute", lambda *a, **k: pl.DataFrame({"datetime": ["invalid"]}),
        )
    caps = CapabilitySet({
        Cap.KLINE_MINUTE_BY_SYMBOL: CapabilityLimits(batch=1, rpm=None),
        Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=50, rpm=None),
    })
    assert kline_sync.fetch_minute_single("600519.SH", date(2026, 9, 30), capset=caps).is_empty()
    assert kline_sync.sync_minute_batch(["600519.SH"]).is_empty()
    if failure in {"missing", "resolve"}:
        assert kline_sync.intraday_monitor_support(caps)["available"] is False


def test_tickflow_partial_catalog_does_not_overwrite(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")

    def fetch(exchange, **kwargs):
        if exchange == "SZ":
            raise RuntimeError("offline")
        return [{"symbol": "600519.SH", "name": "贵州茅台"}]

    monkeypatch.setattr(
        instrument_sync, "get_client",
        lambda: SimpleNamespace(exchanges=SimpleNamespace(get_instruments=fetch)),
    )
    target = tmp_path / "instruments" / "instruments.parquet"
    target.parent.mkdir()
    pl.DataFrame({"symbol": ["000001.SZ"], "name": ["平安银行"]}).write_parquet(target)
    before = target.read_bytes()
    assert instrument_sync.sync_instruments(tmp_path) == 0
    assert target.read_bytes() == before


@pytest.mark.parametrize("key,getter", [
    ("daily_data_provider", preferences.get_daily_data_provider),
    ("adj_factor_provider", preferences.get_adj_factor_provider),
    ("minute_data_provider", preferences.get_minute_data_provider),
    ("full_minute_data_provider", preferences.get_full_minute_data_provider),
    ("depth5_data_provider", preferences.get_depth5_data_provider),
    ("realtime_data_provider", preferences.get_realtime_data_provider),
    ("financial_data_provider", preferences.get_financial_provider),
])
def test_missing_plugin_dependency_preserves_selection(monkeypatch, key, getter):
    monkeypatch.setattr(preferences, "load", lambda: {key: "mootdx"})
    monkeypatch.setattr(custom_sources, "names", lambda: set())
    monkeypatch.setattr(
        custom_sources, "list_plugins", lambda: [{"name": "mootdx", "available": False}],
    )
    assert getter() == "mootdx"


def test_single_adjustment_fetch_follows_selected_provider(selected, monkeypatch):
    expected = pl.DataFrame({
        "symbol": ["600519.SH"], "trade_date": [date(2026, 7, 1)], "ex_factor": [1.05],
    })
    monkeypatch.setattr(selected, "get_adj_factors", lambda *a, **k: expected)
    assert kline_sync.fetch_adj_factor_single("600519.SH").equals(expected)


def test_unavailable_realtime_does_not_replace_cache(selected, monkeypatch):
    from app.services.quote_service import QuoteService

    monkeypatch.setattr(preferences, "get_realtime_data_provider", lambda: "mootdx")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *a: False)
    monkeypatch.setattr(
        "app.tickflow.client.get_paid_realtime_client",
        lambda: pytest.fail("must not use TickFlow"),
    )
    service = QuoteService()
    previous = pl.DataFrame({"symbol": ["000001.SH"], "last_price": [4000.0]})
    service._index_quotes_cache = previous
    assert service._fetch_full_market_quotes() is None
    assert service._index_quotes_cache.equals(previous)


def test_unavailable_financial_source_returns_empty(selected, monkeypatch):
    from app.services.financial_sync import _fetch_table

    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "mootdx")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *a: False)
    monkeypatch.setattr("app.tickflow.client.get_client", lambda: pytest.fail("must not use TickFlow"))
    assert _fetch_table(
        "shares", ["600519.SH"],
        CapabilitySet({Cap.FINANCIAL: CapabilityLimits(batch=50, rpm=None)}),
    ).is_empty()


def test_unavailable_full_minute_source_never_calls_tickflow(tmp_path, selected, monkeypatch):
    from types import SimpleNamespace

    from app.services.minute_refresh import MinuteRefreshService

    monkeypatch.setattr(preferences, "get_full_minute_data_provider", lambda: "mootdx")
    monkeypatch.setattr(custom_sources, "provider_has_dataset", lambda *a: False)
    monkeypatch.setattr(
        kline_sync, "fetch_intraday_full_market_burst",
        lambda *a, **k: pytest.fail("must not use TickFlow"),
    )
    repo = KlineRepository(DataStore(tmp_path))
    try:
        service = MinuteRefreshService(repo)
        service.set_app_state(SimpleNamespace(
            capabilities=CapabilitySet({Cap.INTRADAY_UNIVERSE: CapabilityLimits(rpm=None)}),
        ))
        assert service.capability_ok() is False
        service._run_round()
        assert service._resolve_custom()[1] == "mootdx"
        assert service._state.last_error is not None
    finally:
        repo.db.close()
