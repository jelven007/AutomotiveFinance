"""Data status reports real Parquet coverage without full payload scans."""
from types import SimpleNamespace

import polars as pl

from app.api import data


def _repo(tmp_path):
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        execute_all=lambda _sql: [("symbol",), ("date",), ("close",)],
    )


def _write_partition(root, day, rows, *, timestamp=False):
    directory = root / f"date={day}"
    directory.mkdir(parents=True)
    time_column = (
        pl.Series("datetime", [f"{day} 09:31:00"] * len(rows)).str.to_datetime()
        if timestamp
        else pl.Series("date", [day] * len(rows)).str.to_date()
    )
    pl.DataFrame({"symbol": rows}).with_columns(time_column).write_parquet(
        directory / "part.parquet"
    )


def test_daily_and_enriched_status_report_exact_rows_and_symbols(tmp_path):
    _write_partition(tmp_path / "kline_daily", "2026-01-05", ["A", "B"])
    _write_partition(tmp_path / "kline_daily", "2026-01-06", ["A", "C"])
    _write_partition(tmp_path / "kline_daily_enriched", "2026-01-05", ["A", "B"])
    _write_partition(tmp_path / "kline_daily_enriched", "2026-01-06", ["A", "C"])

    daily = data._safe_aggregate_daily(_repo(tmp_path))
    enriched = data._safe_aggregate_enriched(_repo(tmp_path))

    assert daily == {
        "rows": 4,
        "earliest_date": "2026-01-05",
        "latest_date": "2026-01-06",
        "symbols_covered": 3,
        "trading_days": 2,
    }
    assert enriched == {**daily, "fields": 3}


def test_minute_status_reports_exact_rows_and_symbols(tmp_path):
    _write_partition(
        tmp_path / "kline_minute", "2026-01-05", ["A", "A", "B"], timestamp=True
    )
    _write_partition(
        tmp_path / "kline_minute", "2026-01-06", ["A", "C"], timestamp=True
    )

    assert data._safe_aggregate_minute(_repo(tmp_path)) == {
        "rows": 5,
        "earliest_date": "2026-01-05",
        "latest_date": "2026-01-06",
        "symbols_covered": 3,
        "trading_days": 2,
    }
