"""rustdx provider contracts without requiring a live trading session."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from app.data_providers import custom as custom_sources
from app.plugins.rustdx import client as rustdx_client
from app.plugins.rustdx.client import RustdxClient, RustdxError
from app.plugins.rustdx.provider import RustdxProvider
from app.services import preferences

ROOT = Path(__file__).resolve().parents[2]


class _Native:
    max_connections = 35

    def __init__(self):
        self.quote_calls = []
        self.bar_calls = []

    def quotes_json(self, securities):
        self.quote_calls.append(securities)
        return json.dumps(
            [
                {
                    "market": market,
                    "code": code,
                    "price": 10.5,
                    "last_close": 10,
                    "vol": 123,
                    "amount": 129_150,
                }
                for market, code in securities
            ]
        )

    def bars_json(self, market, code, category, start, count, index):
        self.bar_calls.append((market, code, category, start, count, index))
        return json.dumps(
            [
                {
                    "dt": {"year": 2026, "month": 10, "day": 9, "hour": 15, "minute": 0},
                    "code": code,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "vol": 123,
                    "amount": 129_150,
                }
            ]
        )

    def stats_json(self):
        return json.dumps({"max_connections": 35, "quote_batch_size": 60})


@pytest.fixture
def native(monkeypatch):
    value = _Native()
    monkeypatch.setattr(rustdx_client, "_native_client", lambda: value)
    return value


def test_client_preserves_explicit_exchange_and_native_pool(native):
    client = RustdxClient()

    rows = client.quotes(["000001.SH", "000001.SZ"])

    assert native.quote_calls == [[(1, "000001"), (0, "000001")]]
    assert [(row["market"], row["code"]) for row in rows] == [
        (1, "000001"),
        (0, "000001"),
    ]
    assert client.max_connections == 35
    assert client.stats()["quote_batch_size"] == 60


def test_client_normalizes_rust_datetime_and_index_category(native):
    client = RustdxClient()

    rows = client.bars("000001.SH", frequency=9)

    assert rows[0]["datetime"] == "2026-10-09 15:00"
    assert native.bar_calls == [(1, "000001", 4, 0, 800, True)]


def test_client_normalizes_tdx_zero_sentinel(native):
    native.bars_json = lambda *_args: json.dumps(
        [
            {
                "dt": {"year": 2026, "month": 10, "day": 9, "hour": 14, "minute": 58},
                "code": "600519",
                "vol": 2.0**-127,
                "amount": 2.0**-127,
            }
        ]
    )

    row = RustdxClient().bars("600519.SH", frequency=8)[0]

    assert row["vol"] == 0.0
    assert row["amount"] == 0.0


def test_client_corrects_new_sh_etf_price_coefficient(native):
    native.quotes_json = lambda *_args: json.dumps(
        [
            {
                "market": 1,
                "code": "588200",
                "price": 15.32,
                "last_close": 15.30,
                "bid1": 15.31,
                "ask1": 15.32,
                "vol": 300,
            }
        ]
    )

    row = RustdxClient().quotes(["588200.SH"])[0]

    assert row["price"] == pytest.approx(1.532)
    assert row["last_close"] == pytest.approx(1.53)
    assert row["bid1"] == pytest.approx(1.531)
    assert row["vol"] == 300


def test_rustdx_depth_preserves_five_level_quantities(native):
    native.quotes_json = lambda *_args: json.dumps(
        [
            {
                "market": 1,
                "code": "600519",
                "price": 10,
                **{f"bid{level}_vol": level * 10 for level in range(1, 6)},
                **{f"ask{level}_vol": level * 20 for level in range(1, 6)},
            }
        ]
    )
    depth = RustdxProvider().get_depth_batch(["600519.SH"])["600519.SH"]
    assert depth["bid_volumes"] == [10, 20, 30, 40, 50]
    assert depth["ask_volumes"] == [20, 40, 60, 80, 100]


def test_client_rejects_unsupported_exchange(native):
    with pytest.raises(RustdxError, match="无效交易所"):
        RustdxClient().quotes(["920001.BJ"])


def test_provider_caps_full_market_workers_at_35(monkeypatch):
    monkeypatch.setenv("RUSTDX_WORKERS", "99")
    monkeypatch.setenv("RUSTDX_QUOTE_CONNECTIONS", "99")

    assert RustdxProvider._workers(5_223) == 35
    assert rustdx_client._connection_count() == 35


def test_native_bridge_clamps_direct_constructor_to_35():
    native_module = pytest.importorskip("tsp_rustdx_native")
    assert native_module.RUSTDX_VERSION == "1.12.0"
    assert (
        native_module.RUSTDX_SOURCE_REV
        == "fac771a18cd218c90852254e59b040874d156722"
    )
    client = native_module.RustdxClient(100)
    try:
        assert client.max_connections == 35
        assert json.loads(client.stats_json())["quote_batch_size"] == 60
    finally:
        client.close()


def test_missing_native_bridge_is_isolated(monkeypatch):
    real_import = rustdx_client.importlib.import_module

    def missing(name):
        if name == "tsp_rustdx_native":
            raise ImportError("not installed")
        return real_import(name)

    monkeypatch.setattr(rustdx_client.importlib, "import_module", missing)

    available, reason = rustdx_client.availability()

    assert available is False
    assert "缺少 rustdx 原生桥接" in reason


def test_native_bridge_dependency_mismatch_is_rejected(monkeypatch):
    class StaleNative:
        MAX_CONNECTIONS = 35
        __version__ = "0.1.0"
        RUSTDX_VERSION = "1.11.0"
        RUSTDX_SOURCE_REV = "stale"

    monkeypatch.setattr(
        rustdx_client.importlib,
        "import_module",
        lambda _name: StaleNative,
    )

    available, reason = rustdx_client.availability()

    assert available is False
    assert "依赖版本与项目锁定版本不一致" in reason


def test_provider_uses_one_native_full_market_call():
    calls = []

    class Client:
        def quotes(self, symbols):
            calls.append(symbols)
            return [{"market": 1, "code": "600519", "price": 10}]

        @staticmethod
        def close_shared():
            pass

    provider = RustdxProvider(client_factory=Client)

    assert provider._persistent_quotes(
        ["600519.SH", "000001.SZ"],
        operation="实时行情",
    ) == [{"market": 1, "code": "600519", "price": 10}]
    assert calls == [["600519.SH", "000001.SZ"]]


def test_rustdx_instrument_directory_skips_per_symbol_finance():
    class CatalogClient:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stocks(self, market):
            return (
                [{"code": "000001", "name": "平安银行"}]
                if market == 0
                else [{"code": "600519", "name": "贵州茅台"}]
            )

        def finance(self, _symbol):
            raise AssertionError("rustdx directory must not fetch finance per symbol")

        @staticmethod
        def close_shared():
            pass

    provider = RustdxProvider(client_factory=CatalogClient)

    rows = provider.get_instruments("stock")

    assert [row["symbol"] for row in rows] == ["000001.SZ", "600519.SH"]
    assert all(row["ext"]["float_shares"] is None for row in rows)


def test_rustdx_daily_rows_follow_shared_unit_contract():
    frame = RustdxProvider._daily_frame(
        [
            {
                "datetime": "2026-10-09 15:00",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "vol": 123,
                "amount": 129_150,
            }
        ],
        "600519.SH",
        datetime(2026, 10, 9),
        datetime(2026, 10, 9, 15),
    )

    assert frame["volume"].to_list() == [123.0]


def test_incremental_minutes_request_only_latest_real_bars(native, monkeypatch):
    from app.plugins.rustdx import provider as provider_module

    monkeypatch.setattr(provider_module, "cn_now", lambda: datetime(2026, 10, 9, 15, 1))
    frame = RustdxProvider().get_intraday_latest(["600519.SH", "000001.SZ"], count=3)
    assert len(native.bar_calls) == 2
    assert all(call[2:5] == (8, 0, 3) for call in native.bar_calls)
    assert frame["volume"].to_list() == [1.23, 1.23]
    assert frame["datetime"].to_list() == [datetime(2026, 10, 9, 15)] * 2


def test_plugin_covers_all_core_datasets_and_is_default_provider():
    plugins = {plugin["name"]: plugin for plugin in custom_sources.list_plugins()}

    assert set(plugins["rustdx"]["datasets"]) == {
        "realtime",
        "daily",
        "adj_factor",
        "minute",
        "depth5",
        "financial",
        "full_minute",
    }
    assert preferences._DEFAULT_DATA_PROVIDER == "rustdx"


def test_registry_defaults_to_rustdx_and_retains_tickflow_and_plugins(monkeypatch):
    from app.data_providers.registry import get_provider
    from app.data_providers.tickflow_provider import TickFlowProvider

    assert isinstance(get_provider(), RustdxProvider)
    assert isinstance(get_provider("tickflow"), TickFlowProvider)
    fallback = object()
    calls = []
    monkeypatch.setattr(
        custom_sources, "get_provider", lambda name: calls.append(name) or fallback,
    )
    assert get_provider("fuyao") is fallback
    assert calls == ["fuyao"]


def test_native_bridge_declares_bounded_parallel_quote_batches():
    source = (ROOT / "backend" / "native" / "rustdx_native" / "src" / "lib.rs").read_text(
        encoding="utf-8"
    )

    assert "const MAX_CONNECTIONS: usize = 35;" in source
    assert "const QUOTE_BATCH_SIZE: usize = 60;" in source
    assert ".chunks(QUOTE_BATCH_SIZE)" in source
    assert "max_connections.min(batches.len()).max(1)" in source
    assert "index as u64 * 20" in source
