"""mootdx provider contracts and normalization tests without network access."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.plugins.mootdx.provider import MootdxProvider


class _FakeClient:
    def __init__(self) -> None:
        self.stocks_by_market: dict[int, list[dict]] = {}
        self.bars_by_frequency: dict[int, list[dict]] = {}
        self.quote_rows: list[dict] = []
        self.xdxr_rows: list[dict] = []
        self.finance_rows: dict[str, list[dict]] = {}
        self.history_rows: list[dict] = []
        self.stock_calls: list[int] = []
        self.bar_calls: list[tuple] = []
        self.quote_calls: list[list[str]] = []
        self.history_calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def stocks(self, market):
        self.stock_calls.append(market)
        return self.stocks_by_market.get(market, [])

    def bars(self, code, *, frequency, start=0, offset=800):
        self.bar_calls.append((code, frequency, start, offset))
        return self.bars_by_frequency.get(frequency, []) if start == 0 else []

    def quotes(self, codes):
        self.quote_calls.append(list(codes))
        return [row for row in self.quote_rows if row["code"] in [s.split(".")[0] for s in codes]]

    def xdxr(self, _code):
        return list(self.xdxr_rows)

    def finance(self, code):
        return self.finance_rows.get(code.split(".")[0], [])

    def financial_history(self, symbols, *, periods, columns, cache_dir):
        self.history_calls.append(
            {
                "symbols": symbols,
                "periods": periods,
                "columns": columns,
                "cache_dir": cache_dir,
            }
        )
        return list(self.history_rows)


def _provider(fake: _FakeClient) -> MootdxProvider:
    return MootdxProvider(client_factory=lambda: fake)


def test_instruments_filters_non_a_share_products_and_supports_bj_when_available():
    fake = _FakeClient()
    fake.stocks_by_market = {
        0: [
            {"code": "000001", "name": "平安银行"},
            {"code": "300750", "name": "宁德时代"},
            {"code": "301139", "name": "元道退\x00\x00"},
            {"code": "159001", "name": "货币ETF"},
            {"code": "200001", "name": "深B"},
        ],
        1: [
            {"code": "600519", "name": "贵州茅台"},
            {"code": "688981", "name": "中芯国际"},
            {"code": "510300", "name": "沪深300ETF"},
            {"code": "900901", "name": "沪B"},
        ],
        2: [{"code": "430047", "name": "诺思兰德"}],
    }

    rows = _provider(fake).get_instruments("stock")

    assert [row["symbol"] for row in rows] == [
        "000001.SZ",
        "300750.SZ",
        "430047.BJ",
        "600519.SH",
        "688981.SH",
    ]
    assert fake.stock_calls == [0, 1, 2]
    assert rows[0]["ext"]["tick_size"] == 0.01
    assert rows[0]["ext"]["float_shares"] is None


def test_auction_snapshot_preserves_server_time_and_top_of_book():
    fake = _FakeClient()
    fake.quote_rows = [{
        "market": 1,
        "code": "600519",
        "price": 1501.0,
        "last_close": 1470.0,
        "open": 1500.0,
        "vol": 12_345,
        "amount": 1.86e9,
        "servertime": "09:25:08.125",
        "bid1": 1499.9,
        "bid_vol1": 320,
        "ask1": 1500.0,
        "ask_vol1": 180,
    }]

    rows = _provider(fake).get_auction_snapshot(["600519.SH"])

    assert len(rows) == 1
    assert rows[0]["source_time"] == "09:25:08.125"
    assert rows[0]["bid1"] == pytest.approx(1499.9)
    assert rows[0]["bid1_volume"] == pytest.approx(320)
    assert rows[0]["ask1_volume"] == pytest.approx(180)
    assert fake.quote_calls == [["600519.SH"]]


def test_daily_uses_raw_prices_filters_window_and_preserves_tdx_lots():
    fake = _FakeClient()
    fake.bars_by_frequency[9] = [
        {
            "datetime": "2026-09-30 15:00",
            "open": 1400,
            "high": 1420,
            "low": 1390,
            "close": 1410,
            "vol": 12_345.5,
            "amount": 1.8e9,
        },
        {
            "datetime": "2026-10-01 15:00",
            "open": 1410,
            "high": 1430,
            "low": 1400,
            "close": 1425,
            "vol": 20_000,
            "amount": 2.9e9,
        },
    ]

    frame = _provider(fake).get_daily(
        ["600519.SH"],
        datetime(2026, 9, 30),
        datetime(2026, 9, 30, 23, 59),
    )

    assert frame.columns == [
        "symbol",
        "date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    assert frame.height == 1
    assert frame["date"][0] == date(2026, 9, 30)
    assert frame["volume"][0] == pytest.approx(12_345.5)
    assert frame["amount"][0] == pytest.approx(1.8e9)


def test_minute_keeps_beijing_wallclock_and_converts_tdx_shares(monkeypatch):
    monkeypatch.setenv("MOOTDX_WORKERS", "1")
    fake = _FakeClient()
    fake.bars_by_frequency[8] = [
        {
            "datetime": "2026-10-06 09:31",
            "open": 10.0,
            "high": 10.2,
            "low": 9.9,
            "close": 10.1,
            "vol": 12_300,
            "amount": 124_000,
        }
    ]

    frame = _provider(fake).get_minute(
        ["000001.SZ"],
        datetime(2026, 10, 6, 9, 30),
        datetime(2026, 10, 6, 15),
    )

    assert frame.columns == [
        "symbol",
        "datetime",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    ]
    assert frame["datetime"][0] == datetime(2026, 10, 6, 9, 31)
    assert frame["volume"][0] == pytest.approx(123)


def test_realtime_and_depth_normalize_quote_contracts():
    fake = _FakeClient()
    fake.stocks_by_market = {
        0: [{"code": "000001", "name": "平安银行"}],
        1: [{"code": "600519", "name": "贵州茅台"}],
        2: [],
    }
    fake.quote_rows = [
        {
            "market": 1,
            "code": "600519",
            "price": 1500.0,
            "last_close": 1470.0,
            "open": 1480.0,
            "high": 1510.0,
            "low": 1475.0,
            "vol": 12_345,
            "amount": 1.86e9,
            **{f"ask{level}": 1500.0 + level for level in range(1, 6)},
            **{f"bid{level}": 1500.0 - level for level in range(1, 6)},
            **{f"ask_vol{level}": level * 100 for level in range(1, 6)},
            **{f"bid_vol{level}": level * 200 for level in range(1, 6)},
        }
    ]
    provider = _provider(fake)

    quote = provider.get_realtime()[0]
    depth = provider.get_depth_batch(["600519.SH"])["600519.SH"]

    assert quote["name"] == "贵州茅台"
    assert quote["volume"] == pytest.approx(12_345)
    assert quote["change_amount"] == pytest.approx(30)
    assert quote["change_pct"] == pytest.approx(30 / 1470)
    assert quote["amplitude"] is None
    assert depth["ask_prices"] == [1501, 1502, 1503, 1504, 1505]
    assert depth["bid_volumes"] == [200, 400, 600, 800, 1000]


def test_adjustment_factor_uses_per_ten_share_event_and_previous_raw_close():
    fake = _FakeClient()
    fake.xdxr_rows = [
        {
            "year": 2026,
            "month": 7,
            "day": 1,
            "category": 1,
            "fenhong": 2.0,
            "songzhuangu": 1.0,
            "peigu": 0.0,
            "peigujia": 0.0,
        }
    ]
    fake.bars_by_frequency[9] = [
        {
            "datetime": "2026-06-30 15:00",
            "open": 9.8,
            "high": 10.1,
            "low": 9.7,
            "close": 10.0,
            "vol": 100_000,
            "amount": 1_000_000,
        },
        {
            "datetime": "2026-07-01 15:00",
            "open": 8.9,
            "high": 9.1,
            "low": 8.8,
            "close": 9.0,
            "vol": 100_000,
            "amount": 900_000,
        },
    ]

    frame = _provider(fake).get_adj_factors(
        ["600519.SH"],
        datetime(2026, 1, 1),
        datetime(2026, 12, 31),
    )

    # (10 - 0.2) / (1 + 0.1) = 8.909.., exchange half-up reference = 8.91.
    assert frame.height == 1
    assert frame["trade_date"][0] == date(2026, 7, 1)
    assert frame["ex_factor"][0] == pytest.approx(10 / 8.91)


def test_financial_history_maps_report_and_announcement_dates():
    fake = _FakeClient()
    fake.history_rows = [
        {
            "code": "600519",
            "report_date": "20240630",
            "col314": 240809,
            "col1": 35.57,
            "col4": 201.0,
            "col281": 16.75,
            "col202": 91.3,
            "col199": 50.7,
            "col183": 17.6,
            "col184": 15.8,
            "col210": 18.2,
            "col238": 1_256_197_800,
            "col239": 1_256_197_800,
        }
    ]
    provider = _provider(fake)

    metrics = provider.get_financials("metrics", ["600519.SH"], latest_only=False)
    shares = provider.get_financials("shares", ["600519.SH"], latest_only=False)

    row = metrics.to_dicts()[0]
    assert row["period_end"] == "2024-06-30"
    assert row["announce_date"] == "2024-08-09"
    assert row["eps_basic"] == pytest.approx(35.57)
    assert row["roe"] == pytest.approx(16.75)
    assert row["debt_to_asset_ratio"] == pytest.approx(18.2)
    assert shares["float_shares"][0] == pytest.approx(1_256_197_800)
    assert fake.history_calls[0]["periods"] == 8


def test_financial_snapshot_without_report_period_is_not_used_as_history():
    fake = _FakeClient()
    fake.finance_rows["600519"] = [
        {
            "updated_date": 20240630,
            "zongguben": 1_000_000_000,
            "liutongguben": 800_000_000,
            "zongzichan": 300_000_000_000,
            "liudongfuzhai": 30_000_000_000,
            "changqifuzhai": 10_000_000_000,
            "jingzichan": 260_000_000_000,
            "zhuyingshouru": 100_000_000_000,
            "zhuyinglirun": 90_000_000_000,
            "jinglirun": 50_000_000_000,
            "meigujingzichan": 260,
        }
    ]

    frame = _provider(fake).get_financials("metrics", ["600519.SH"], latest_only=True)

    assert frame.is_empty()


def test_full_minute_uses_same_real_bar_path(monkeypatch):
    monkeypatch.setenv("MOOTDX_WORKERS", "1")
    monkeypatch.setattr(
        "app.plugins.mootdx.provider.cn_now", lambda: datetime(2026, 10, 6, 15)
    )
    fake = _FakeClient()
    fake.bars_by_frequency[8] = [
        {
            "datetime": "2026-10-06 10:00",
            "open": 10,
            "high": 10.1,
            "low": 9.9,
            "close": 10,
            "vol": 10_000,
            "amount": 100_000,
        }
    ]
    provider = _provider(fake)

    frame = provider.get_intraday_batch(["000001.SZ"])

    assert frame.height == 1
    assert fake.bar_calls[0][1] == 8


def test_plugin_manifest_declares_all_routable_datasets():
    from app.data_providers import custom as custom_sources

    plugins = {plugin["name"]: plugin for plugin in custom_sources.list_plugins()}
    assert "mootdx" in plugins
    assert set(plugins["mootdx"]["datasets"]) == {
        "realtime",
        "daily",
        "adj_factor",
        "minute",
        "depth5",
        "financial",
        "full_minute",
    }
    assert custom_sources.is_builtin("mootdx")


def test_missing_depth_volume_stays_unknown():
    fake = _FakeClient()
    fake.quote_rows = [{"market": 1, "code": "600519", "ask_vol1": 0}]
    depth = _provider(fake).get_depth_batch(["600519.SH"])["600519.SH"]
    assert depth["ask_volumes"] == [0, None, None, None, None]
    assert depth["bid_volumes"] == [None] * 5


def test_index_and_stock_with_same_code_keep_exchange():
    fake = _FakeClient()
    fake.quote_rows = [
        {"market": 1, "code": "000001", "price": 4000},
        {"market": 0, "code": "000001", "price": 12},
    ]
    rows = _provider(fake).get_realtime_indices(["000001.SH"])
    assert [row["symbol"] for row in rows] == ["000001.SH"]
    assert rows[0]["last_price"] == 4000
    assert fake.quote_calls == [["000001.SH"]]


def test_utc_window_is_converted_before_minute_filtering(monkeypatch):
    monkeypatch.setenv("MOOTDX_MAX_PAGES", "invalid")
    fake = _FakeClient()
    fake.bars_by_frequency[8] = [
        {"datetime": "2026-09-30 09:31", "close": 10, "vol": 100, "amount": 100_000}
    ]
    frame = _provider(fake).get_minute(
        ["000001.SZ"],
        datetime(2026, 9, 30, 1, 30, tzinfo=UTC),
        datetime(2026, 9, 30, 1, 32, tzinfo=UTC),
    )
    assert frame.height == 1
    assert frame["datetime"][0] == datetime(2026, 9, 30, 9, 31)


def test_financial_cumulative_fields_zero_and_unknown_announcement():
    fake = _FakeClient()
    fake.history_rows = [
        {
            "code": "600519", "report_date": "20240630", "col314": 240809,
            "col74": 1000, "col95": 100, "col96": 90, "col77": 0,
            "col80": -1e38, "col232": 40,  # later quarterly field must not replace YTD
        },
        {"code": "000001", "report_date": "20240630", "col238": 1000, "col239": 800},
    ]
    provider = _provider(fake)
    income = provider.get_financials("income", ["600519.SH"], latest_only=False).to_dicts()[0]
    assert income["net_income"] == 100
    assert income["selling_expense"] == 0
    assert income["financial_expense"] is None
    assert provider.get_financials("shares", ["000001.SZ"], latest_only=False).is_empty()


def test_financial_failure_is_retried_and_success_cache_expires(monkeypatch):
    fake = _FakeClient()
    clock = [0.0]
    monkeypatch.setattr("app.plugins.mootdx.provider.time.monotonic", lambda: clock[0])
    provider = _provider(fake)
    assert provider.get_financials("metrics", ["600519.SH"]).is_empty()
    fake.history_rows = [
        {"code": "600519", "report_date": "20240630", "col314": 240809, "col1": 30}
    ]
    assert provider.get_financials("metrics", ["600519.SH"])["eps_basic"][0] == 30
    fake.history_rows = [
        {"code": "600519", "report_date": "20240630", "col314": 240809, "col1": 31}
    ]
    assert provider.get_financials("metrics", ["600519.SH"])["eps_basic"][0] == 30
    clock[0] = 3601
    assert provider.get_financials("metrics", ["600519.SH"])["eps_basic"][0] == 31


def test_same_day_adjustment_events_are_combined(monkeypatch):
    monkeypatch.setattr("app.plugins.mootdx.provider.cn_now", lambda: datetime(2026, 7, 2))
    fake = _FakeClient()
    fake.xdxr_rows = [
        {"year": 2026, "month": 7, "day": 1, "category": 1, "fenhong": 2},
        {"year": 2026, "month": 7, "day": 1, "category": 1, "fenhong": 3},
        {"year": 2027, "month": 7, "day": 1, "category": 1, "fenhong": 5},
    ]
    fake.bars_by_frequency[9] = [
        {"datetime": "2026-06-30 15:00", "close": 10},
        {"datetime": "2026-07-01 15:00", "close": 9.5},
    ]
    frame = _provider(fake).get_adj_factors(["600519.SH"], None, None)
    assert frame.height == 1
    assert frame["ex_factor"][0] == pytest.approx(10 / 9.5)


def test_repeated_full_bar_page_stops_pagination(monkeypatch):
    monkeypatch.setenv("MOOTDX_MAX_PAGES", "80")
    fake = _FakeClient()
    calls = []

    def repeat(*args, **kwargs):
        calls.append(kwargs["start"])
        return [{
            "datetime": "2026-09-30 15:00", "open": 10, "high": 10,
            "low": 10, "close": 10, "vol": 100, "amount": 100_000,
        }] * 800

    fake.bars = repeat
    frame = _provider(fake).get_daily(
        ["600519.SH"], datetime(2026, 1, 1), datetime(2026, 10, 1),
    )
    assert frame.height == 1
    assert calls == [0, 800]


def test_instruments_get_listing_and_current_share_capital():
    fake = _FakeClient()
    fake.stocks_by_market = {
        0: [{"code": "000001", "name": "平安银行"}],
        1: [{"code": "600519", "name": "贵州茅台"}],
    }
    fake.finance_rows["600519"] = [{
        "ipo_date": 20010827, "liutongguben": 1_250_081_562.5, "zongguben": 1_250_081_562.5,
    }]
    provider = _provider(fake)
    row = provider.get_instruments()[1]
    assert row["ext"]["listing_date"] == "2001-08-27"
    assert row["ext"]["float_shares"] == 1_250_081_562.5
    row["ext"]["float_shares"] = 0
    assert provider.get_instruments()[1]["ext"]["float_shares"] == 1_250_081_562.5


def test_partial_instrument_catalog_is_not_published_or_cached():
    fake = _FakeClient()
    fake.stocks_by_market = {1: [{"code": "600519", "name": "贵州茅台"}]}
    provider = _provider(fake)
    assert provider.get_instruments("stock") == []
    fake.stocks_by_market[0] = [{"code": "000001", "name": "平安银行"}]
    assert [row["symbol"] for row in provider.get_instruments("stock")] == [
        "000001.SZ", "600519.SH",
    ]


def test_client_rejects_truncated_security_list():
    from app.plugins.mootdx.client import MootdxClient, MootdxError

    client = MootdxClient()
    pages = iter([[{"code": "600519"}], []])
    client._invoke = lambda method, **kwargs: 1001 if method == "stock_count" else next(pages)
    with pytest.raises(MootdxError, match="不完整"):
        client.stocks(1)


@pytest.mark.parametrize("dataset", ["daily", "minute"])
def test_protocol_outage_stops_after_three_failures_but_preserves_received_rows(dataset, monkeypatch):
    from app.plugins.mootdx.client import MootdxError

    monkeypatch.setenv("MOOTDX_WORKERS", "1")
    fake = _FakeClient()
    calls = []

    def bars(symbol, **kwargs):
        calls.append(symbol)
        if symbol != "600000.SH":
            raise MootdxError("offline")
        return [{
            "datetime": "2026-09-30 15:00", "open": 10, "high": 10,
            "low": 10, "close": 10, "vol": 100, "amount": 100_000,
        }]

    fake.bars = bars
    provider = _provider(fake)
    frame = getattr(provider, f"get_{dataset}")(
        [f"{600000 + i}.SH" for i in range(20)],
        datetime(2026, 9, 30), datetime(2026, 9, 30, 15),
    )
    assert len(calls) == 4
    assert frame["symbol"].to_list() == ["600000.SH"]


def test_empty_trial_reports_no_data_and_can_recover():
    fake = _FakeClient()
    provider = _provider(fake)
    empty = provider.test_dataset("realtime", ["600519.SH"])
    assert empty["rows"] == 0 and "未获取到数据" in empty["error"]
    fake.quote_rows = [{"code": "600519", "market": 1, "price": 1258.62}]
    valid = provider.test_dataset("realtime", ["600519.SH"])
    assert valid["rows"] == 1 and "error" not in valid


def test_etf_dividend_reference_keeps_mill_precision():
    fake = _FakeClient()
    fake.xdxr_rows = [
        {"year": 2026, "month": 7, "day": 1, "category": 1, "fenhong": 0.04}
    ]
    fake.bars_by_frequency[9] = [
        {"datetime": "2026-06-30 15:00", "close": 1.08},
        {"datetime": "2026-07-01 15:00", "close": 1.076},
    ]
    factors = _provider(fake).get_adj_factors(["588200.SH"], None, None, asset_type="etf")
    assert factors["ex_factor"][0] == pytest.approx(1.08 / 1.076)
