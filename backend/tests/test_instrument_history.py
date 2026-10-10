from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.backtest.matrix import (
    build_basic_filter_mask,
    build_market_data_matrix,
    load_market_data_matrix_from_parquet,
)
from app.indicators.pipeline import compute_limit_signals
from app.instrument_history import (
    InstrumentHistoryError,
    attach_instrument_history,
    load_instrument_history,
    update_instrument_history,
)
from app.services import instrument_sync
from app.strategy import paper


def _instruments(*rows: tuple[str, str, str]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": [row[0] for row in rows],
            "name": [row[1] for row in rows],
            "listing_date": [row[2] for row in rows],
        }
    )


def test_scd2_history_tracks_changes_and_delisting_tombstones(tmp_path) -> None:
    first = date(2026, 7, 1)
    second = date(2026, 7, 2)

    assert update_instrument_history(
        tmp_path,
        _instruments(
            ("600001.SH", "示例股份", "2020-01-01"),
            ("000001.SZ", "平安银行", "1991-04-03"),
        ),
        as_of=first,
        source="mootdx",
        available_at="2026-07-01T01:10:00+00:00",
    ) == 2
    assert update_instrument_history(
        tmp_path,
        _instruments(
            ("600001.SH", "*ST示例", "2020-01-01"),
            ("300001.SZ", "特锐德", "2009-10-30"),
        ),
        as_of=second,
        source="mootdx",
        available_at="2026-07-02T01:10:00+00:00",
    ) == 3

    history = load_instrument_history(tmp_path)
    example = history.filter(pl.col("symbol") == "600001.SH").sort("valid_from")
    assert example["name"].to_list() == ["示例股份", "*ST示例"]
    assert example["is_risk_warning"].to_list() == [False, True]
    assert example["valid_to"].to_list() == [second, None]

    delisted = history.filter(
        (pl.col("symbol") == "000001.SZ")
        & (pl.col("valid_from") == second)
    ).row(0, named=True)
    assert delisted["is_listed"] is False
    assert delisted["name"] == "平安银行"


def test_same_day_refresh_replaces_that_days_observation(tmp_path) -> None:
    day = date(2026, 7, 1)
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "示例股份", "2020-01-01")),
        as_of=day,
        source="mootdx",
        available_at="2026-07-01T01:10:00+00:00",
    )
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "*ST示例", "2020-01-01")),
        as_of=day,
        source="mootdx",
        available_at="2026-07-01T01:12:00+00:00",
    )

    history = load_instrument_history(tmp_path)
    assert history.height == 1
    assert history["name"][0] == "*ST示例"
    assert history["is_risk_warning"][0] is True
    assert history["available_at"][0] == "2026-07-01T01:12:00+00:00"


def test_asof_join_never_uses_future_status(tmp_path) -> None:
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "示例股份", "2020-01-01")),
        as_of=date(2026, 7, 2),
        source="mootdx",
        available_at="2026-07-02T01:10:00+00:00",
    )
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "*ST示例", "2020-01-01")),
        as_of=date(2026, 7, 6),
        source="mootdx",
        available_at="2026-07-06T01:10:00+00:00",
    )
    rows = pl.DataFrame(
        {
            "symbol": ["600001.SH"] * 3,
            "date": [
                date(2026, 7, 1),
                date(2026, 7, 3),
                date(2026, 7, 6),
            ],
        }
    )

    resolved = attach_instrument_history(rows, load_instrument_history(tmp_path))

    assert resolved["_pit_known"].to_list() == [False, True, True]
    assert resolved["_pit_name"].to_list() == [None, "示例股份", "*ST示例"]
    assert resolved["_pit_is_risk_warning"].to_list() == [None, False, True]


def test_out_of_order_snapshot_is_rejected(tmp_path) -> None:
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "示例股份", "2020-01-01")),
        as_of=date(2026, 7, 2),
        source="mootdx",
    )

    with pytest.raises(InstrumentHistoryError, match="predates"):
        update_instrument_history(
            tmp_path,
            _instruments(("600001.SH", "示例股份", "2020-01-01")),
            as_of=date(2026, 7, 1),
            source="mootdx",
        )


def test_instrument_sync_persists_current_and_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        instrument_sync,
        "_fetch_instruments_via_provider",
        lambda: [{"symbol": "600001.SH", "name": "*ST示例", "listing_date": "2020-01-01"}],
    )
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: date(2026, 7, 2))
    monkeypatch.setattr(
        "app.services.preferences.get_daily_data_provider",
        lambda: "mootdx",
    )

    assert instrument_sync.sync_instruments(tmp_path) == 1
    current = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    history = load_instrument_history(tmp_path)

    assert current["as_of"][0] == date(2026, 7, 2)
    assert history.select(
        "symbol", "valid_from", "is_risk_warning", "is_listed", "source"
    ).row(0) == ("600001.SH", date(2026, 7, 2), True, True, "mootdx")


def test_daily_stock_pool_replaces_current_rows_and_tombstones_removed_symbols(
    tmp_path,
    monkeypatch,
) -> None:
    state = {
        "day": date(2026, 7, 1),
        "rows": [
            {"symbol": "600001.SH", "name": "沪市主板"},
            {"symbol": "300001.SZ", "name": "创业板"},
            {"symbol": "510300.SH", "name": "沪深300ETF"},
            {"symbol": "430047.BJ", "name": "北交所样本"},
            {"symbol": "600002.SH", "name": "退市样本"},
        ],
    }
    monkeypatch.setattr(
        instrument_sync,
        "_fetch_instruments_via_provider",
        lambda: state["rows"],
    )
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: state["day"])
    monkeypatch.setattr(
        "app.services.preferences.get_daily_data_provider",
        lambda: "mootdx",
    )

    assert instrument_sync.sync_instruments(tmp_path) == 2
    current_path = tmp_path / "instruments" / "instruments.parquet"
    assert pl.read_parquet(current_path)["symbol"].to_list() == [
        "300001.SZ",
        "600001.SH",
    ]

    state["day"] = date(2026, 7, 2)
    state["rows"] = [{"symbol": "300001.SZ", "name": "创业板"}]
    assert instrument_sync.sync_instruments(tmp_path) == 1
    assert pl.read_parquet(current_path)["symbol"].to_list() == ["300001.SZ"]

    removed = load_instrument_history(tmp_path).filter(
        (pl.col("symbol") == "600001.SH")
        & (pl.col("valid_from") == date(2026, 7, 2))
    ).row(0, named=True)
    assert removed["is_listed"] is False


def test_quote_name_enrichment_updates_same_day_history(tmp_path, monkeypatch) -> None:
    current_path = tmp_path / "instruments" / "instruments.parquet"
    current_path.parent.mkdir(parents=True)
    pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "name": [""],
            "listing_date": [date(2020, 1, 1)],
            "as_of": [date(2026, 7, 2)],
        }
    ).write_parquet(current_path)
    monkeypatch.setattr(
        "app.services.preferences.get_daily_data_provider",
        lambda: "mootdx",
    )
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: date(2026, 7, 2))

    assert instrument_sync.enrich_names_from_quotes(
        tmp_path,
        [{"symbol": "600001.SH", "name": "*ST示例"}],
    ) == 1

    history = load_instrument_history(tmp_path)
    assert history.height == 1
    assert history["name"][0] == "*ST示例"
    assert history["is_risk_warning"][0] is True


def test_limit_signals_use_status_effective_on_each_trade_date(tmp_path) -> None:
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "示例股份", "2020-01-01")),
        as_of=date(2026, 7, 2),
        source="mootdx",
    )
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "*ST示例", "2020-01-01")),
        as_of=date(2026, 7, 3),
        source="mootdx",
    )
    bars = pl.DataFrame(
        {
            "symbol": ["600001.SH"] * 3,
            "date": [date(2026, 7, 1), date(2026, 7, 2), date(2026, 7, 3)],
            "open": [10.0, 10.5, 11.03],
            "high": [10.0, 10.5, 11.03],
            "low": [10.0, 10.5, 11.03],
            "close": [10.0, 10.5, 11.03],
            "raw_close": [10.0, 10.5, 11.03],
            "raw_high": [10.0, 10.5, 11.03],
            "raw_low": [10.0, 10.5, 11.03],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )
    current = pl.DataFrame(
        {
            "symbol": ["600001.SH"],
            "name": ["*ST示例"],
            "listing_date": [date(2020, 1, 1)],
        }
    )

    result = compute_limit_signals(
        bars,
        current,
        needed={"signal_limit_up"},
        instrument_history=load_instrument_history(tmp_path),
        include_instrument_metadata=True,
    )

    assert result["instrument_status_known"].to_list() == [False, True, True]
    assert result["is_risk_warning"].to_list() == [True, False, True]
    assert result["name"].to_list() == ["*ST示例", "示例股份", "*ST示例"]
    assert result["signal_limit_up"].to_list() == [None, False, True]


def test_matrix_basic_filter_uses_row_level_risk_warning_state() -> None:
    panel = pl.DataFrame(
        {
            "symbol": ["600001.SH", "600001.SH"],
            "date": [date(2026, 7, 2), date(2026, 7, 3)],
            "open": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "close": [10.0, 10.0],
            "volume": [1000.0, 1000.0],
            "is_risk_warning": [False, True],
        }
    )

    market = build_market_data_matrix(
        panel,
        field_columns={"is_risk_warning"},
    )
    mask = build_basic_filter_mask(
        market,
        {"enabled": True, "exclude_st": True},
    )

    assert mask[:, 0].tolist() == [True, False]


def test_parquet_matrix_overlays_history_on_legacy_schema(tmp_path) -> None:
    root = tmp_path / "enriched"
    for day in (date(2026, 7, 2), date(2026, 7, 3)):
        target = root / f"date={day.isoformat()}"
        target.mkdir(parents=True)
        pl.DataFrame(
            {
                "symbol": ["600001.SH"],
                "date": [day],
                "open": [10.0],
                "high": [10.0],
                "low": [10.0],
                "close": [10.0],
                "volume": [1000.0],
            }
        ).write_parquet(target / "part.parquet")
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "示例股份", "2020-01-01")),
        as_of=date(2026, 7, 2),
        source="mootdx",
    )
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "*ST示例", "2020-01-01")),
        as_of=date(2026, 7, 3),
        source="mootdx",
    )

    market = load_market_data_matrix_from_parquet(
        root,
        date(2026, 7, 2),
        date(2026, 7, 3),
        field_columns={"is_risk_warning"},
        instruments=pl.DataFrame(
            {"symbol": ["600001.SH"], "name": ["*ST示例"]}
        ),
        instrument_history=load_instrument_history(tmp_path),
    )
    mask = build_basic_filter_mask(
        market,
        {"enabled": True, "exclude_st": True},
    )

    assert mask[:, 0].tolist() == [True, False]


def test_paper_limit_uses_status_effective_on_fill_date(tmp_path) -> None:
    update_instrument_history(
        tmp_path,
        _instruments(("600001.SH", "*ST示例", "2020-01-01")),
        as_of=date(2026, 7, 3),
        source="mootdx",
    )

    assert paper._risk_warning_on(tmp_path, "600001.SH", date(2026, 7, 3)) is True
    assert paper.limit_prices(
        10.0,
        "600001.SH",
        "stock",
        trade_date=date(2026, 7, 3),
        is_risk_warning=True,
    ) == (10.5, 9.5)
