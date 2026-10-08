from __future__ import annotations

import json
from datetime import date

import polars as pl
import pytest

from app.enriched_generation import EnrichedGenerationUnavailableError, get_enriched_generation
from app.indicators import pipeline
from app.services import index_sync
from app.tickflow.repository import DataStore, KlineRepository


def _seed(tmp_path, asset):
    daily = "kline_daily" if asset == "stock" else "kline_etf_daily"
    enriched = "kline_daily_enriched" if asset == "stock" else "kline_etf_enriched"
    for day in (14, 15):
        frame = pl.DataFrame({
            "symbol": ["600000.SH", "600001.SH"] if asset == "stock" else ["510300.SH", "510500.SH"],
            "date": [date(2026, 8, day)] * 2,
            "open": [10.0, 11.0], "high": [10.0, 11.0],
            "low": [10.0, 11.0], "close": [10.0, 11.0],
            "volume": [1000.0, 1000.0], "amount": [10000.0, 11000.0],
        })
        raw_path = tmp_path / daily / f"date=2026-08-{day}" / "part.parquet"
        old_path = tmp_path / enriched / f"date=2026-08-{day}" / "part.parquet"
        raw_path.parent.mkdir(parents=True)
        old_path.parent.mkdir(parents=True)
        frame.write_parquet(raw_path)
        frame.with_columns(pl.lit(1.0).alias("close")).write_parquet(old_path)
    (tmp_path / f".matrix_generation_{asset}.json").write_text(json.dumps({
        "state": "publishing", "generation": "old", "owner_pid": 999999999,
    }))
    return tmp_path / enriched


def _recover(tmp_path, asset):
    if asset == "stock":
        # 失败恢复应忽略局部请求, 强制覆盖全部标的、全部日期。
        return pipeline.run_pipeline(tmp_path, symbols=["600000.SH"], new_dates_only=True)
    return index_sync.rebuild_etf_enriched(KlineRepository(DataStore(tmp_path)))


@pytest.mark.parametrize("asset", ["stock", "etf"])
def test_full_recovery_rewrites_all_partitions(tmp_path, asset):
    root = _seed(tmp_path, asset)
    assert _recover(tmp_path, asset) == 4
    assert get_enriched_generation(tmp_path, asset) != "old"
    for path in root.glob("date=*/*.parquet"):
        assert pl.read_parquet(path).sort("symbol")["close"].to_list() == [10.0, 11.0]


@pytest.mark.parametrize("asset", ["stock", "etf"])
def test_failed_recovery_remains_closed_and_can_retry(tmp_path, monkeypatch, asset):
    _seed(tmp_path, asset)
    from app.enriched_generation import EnrichedPublication

    original = EnrichedPublication.write_parquet

    def fail_second(self, frame, out):
        if "2026-08-15" in str(out):
            raise OSError("injected second partition failure")
        return original(self, frame, out)

    with monkeypatch.context() as patch:
        patch.setattr(EnrichedPublication, "write_parquet", fail_second)
        with pytest.raises(OSError, match="injected"):
            _recover(tmp_path, asset)
    with pytest.raises(EnrichedGenerationUnavailableError):
        get_enriched_generation(tmp_path, asset)
    assert _recover(tmp_path, asset) == 4
    assert get_enriched_generation(tmp_path, asset)


def test_etf_recovery_rejects_incomplete_daily_history(tmp_path):
    root = _seed(tmp_path, "etf")
    before = (root / "date=2026-08-14" / "part.parquet").read_bytes()
    (tmp_path / "kline_etf_daily" / "date=2026-08-15" / "part.parquet").unlink()
    with pytest.raises(RuntimeError, match="缺少已有日期分区"):
        _recover(tmp_path, "etf")
    assert (root / "date=2026-08-14" / "part.parquet").read_bytes() == before
    with pytest.raises(EnrichedGenerationUnavailableError):
        get_enriched_generation(tmp_path, "etf")
