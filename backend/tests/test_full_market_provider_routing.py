"""Whole-market jobs must retain the selected source and bound minute batches."""
from types import SimpleNamespace

import polars as pl
import pytest

from app.config import settings
from app.data_providers import custom
from app.services import kline_sync, preferences
from app.tickflow import pools


@pytest.mark.parametrize("pool_id,asset", [("CN_Equity_A", "stock"), ("CN_Index", "index")])
def test_custom_universe_ignores_tickflow_cache_and_client(tmp_path, monkeypatch, pool_id, asset):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "rustdx")
    target = tmp_path / "pools" / f"{pool_id}.parquet"
    target.parent.mkdir()
    pl.DataFrame({"symbol": ["stale-source"]}).write_parquet(target)
    calls = []

    def instruments(asset_type):
        calls.append(asset_type)
        return [{"symbol": "600519.SH"}, {"symbol": "600519.SH"}]

    monkeypatch.setattr(custom, "get_provider", lambda _: SimpleNamespace(get_instruments=instruments))
    monkeypatch.setattr(pools, "get_client", lambda: pytest.fail("cross-source request"))
    assert pools.get_pool(pool_id) == ["600519.SH"]
    assert pools.get_pool(pool_id, refresh=True) == ["600519.SH"]
    assert calls == [asset, asset]
    assert pl.read_parquet(target)["symbol"].to_list() == ["stale-source"]


def test_unavailable_custom_universe_never_falls_back(monkeypatch):
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "rustdx")

    def unavailable(_):
        raise ValueError("not installed")

    monkeypatch.setattr(custom, "get_provider", unavailable)
    monkeypatch.setattr(pools, "get_client", lambda: pytest.fail("cross-source request"))
    assert pools.get_pool("CN_Equity_A") == []


def test_whole_market_custom_minutes_flush_bounded_chunks(monkeypatch):
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "rustdx")
    calls, segments, progress = [], [], []

    def fetch(symbols, **kwargs):
        calls.append(list(symbols))
        return pl.DataFrame({"symbol": symbols}), False

    monkeypatch.setattr(kline_sync, "_try_custom_minute", fetch)
    symbols = [f"{600000 + i}.SH" for i in range(251)]
    result = kline_sync.sync_minute_batch(
        symbols, batch_size=5000, on_segment=segments.append,
        on_chunk_done=lambda *args: progress.append(args),
    )
    assert result.is_empty()
    assert [len(chunk) for chunk in calls] == [100, 100, 51]
    assert [frame.height for frame in segments] == [100, 100, 51]
    assert pl.concat(segments)["symbol"].to_list() == symbols
    assert progress[-1][:2] == (3, 3)


def test_custom_minute_source_change_aborts_without_tickflow(monkeypatch):
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "rustdx")
    monkeypatch.setattr(kline_sync, "_try_custom_minute", lambda *a, **k: (None, True))
    monkeypatch.setattr(kline_sync, "get_client", lambda: pytest.fail("cross-source request"))
    with pytest.raises(RuntimeError, match="数据源"):
        kline_sync.sync_minute_batch(
            [f"{600000 + i}.SH" for i in range(101)], on_segment=lambda _: None,
        )
