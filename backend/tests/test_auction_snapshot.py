"""Full-market 09:25 auction snapshot collection."""

from __future__ import annotations

import json
from datetime import date, datetime

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import abnormal
from app.jobs import daily_pipeline
from app.market_time import CN_TZ
from app.services import auction_snapshot, instrument_sync

DAY = date(2026, 10, 9)
NOW = datetime(2026, 10, 9, 9, 25, 12, tzinfo=CN_TZ)


class _Provider:
    name = "mootdx"

    def __init__(self, *, source_time: str = "09:25:10.000", prev_close: float = 10.0):
        self.calls: list[list[str]] = []
        self.source_time = source_time
        self.prev_close = prev_close

    def get_auction_snapshot(self, symbols: list[str]) -> list[dict]:
        self.calls.append(symbols)
        return [
            {
                "symbol": symbol,
                "last_price": 10.5 + index * 11,
                "open": 10.5 + index * 11,
                "prev_close": self.prev_close + index * 10,
                "volume": 1000.0 + index,
                "amount": 1_050_000.0 + index,
                "bid1": 10.49 + index * 11,
                "bid1_volume": 120.0,
                "ask1": 10.5 + index * 11,
                "ask1_volume": 80.0,
                "source_time": self.source_time,
            }
            for index, symbol in enumerate(symbols)
        ]


def test_instrument_normalization_drops_delisted_labels():
    rows = instrument_sync._flatten_instruments([
        {"symbol": "000001.SZ", "name": "平安银行\x00"},
        {"symbol": "301139.SZ", "name": "元道退\x00\x00"},
        {"symbol": "600001.SH", "name": "退市样本"},
        {"symbol": "600002.SH", "name": "样本摘牌"},
    ])

    assert rows == [{
        "symbol": "000001.SZ",
        "name": "平安银行",
        "code": None,
        "exchange": None,
        "region": None,
        "type": None,
        "listing_date": None,
        "total_shares": None,
        "float_shares": None,
        "tick_size": None,
        "limit_up": None,
        "limit_down": None,
    }]


def _seed(data_dir, *, as_of: date = DAY) -> None:
    instruments = data_dir / "instruments" / "instruments.parquet"
    instruments.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH", "301139.SZ"],
        "name": ["平安银行", "浦发银行", "元道退\x00\x00"],
        "exchange": ["SZ", "SH", "SZ"],
        "type": ["stock", "stock", "stock"],
        "as_of": [as_of, as_of, as_of],
    }).write_parquet(instruments)

    daily = data_dir / "kline_daily" / "date=2026-09-30" / "part.parquet"
    daily.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["000001.SZ", "600000.SH"],
        "close": [10.0, 20.0],
    }).write_parquet(daily)


def _capture(monkeypatch, tmp_path, provider: _Provider) -> dict:
    monkeypatch.setattr(auction_snapshot.preferences, "get_realtime_data_provider", lambda: "mootdx")
    monkeypatch.setattr(auction_snapshot.custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(auction_snapshot.custom_sources, "get_provider", lambda name: provider)
    monkeypatch.setattr(auction_snapshot.trading_day, "is_trading_day", lambda now: True)
    return auction_snapshot.capture_auction_snapshot(tmp_path, now=NOW)


def test_capture_uses_fresh_active_catalog_and_publishes_atomically(tmp_path, monkeypatch):
    _seed(tmp_path)
    provider = _Provider()

    result = _capture(monkeypatch, tmp_path, provider)

    assert result["state"] == "ready"
    assert result["universe_count"] == 2
    assert result["quote_count"] == 2
    assert result["matched_count"] == 2
    assert provider.calls == [["000001.SZ", "600000.SH"]]

    root = tmp_path / "auction_snapshot" / f"date={DAY.isoformat()}"
    frame = pl.read_parquet(root / "part.parquet")
    assert frame["symbol"].to_list() == ["000001.SZ", "600000.SH"]
    assert frame["auction_change_pct"].to_list() == pytest.approx([0.05, 0.075])
    assert frame["order_imbalance"].to_list() == pytest.approx([0.2, 0.2])
    metadata = json.loads((root / "metadata.json").read_text())
    assert metadata["capture_id"] == frame["capture_id"][0]
    assert metadata["universe_as_of"] == DAY.isoformat()

    payload = auction_snapshot.get_auction_snapshot(tmp_path)
    assert payload["state"] == "ready"
    assert payload["available_dates"] == [DAY.isoformat()]
    assert [row["symbol"] for row in payload["rows"]] == ["600000.SH", "000001.SZ"]


def test_capture_rejects_stale_catalog_without_network_request(tmp_path, monkeypatch):
    _seed(tmp_path, as_of=date(2026, 9, 30))
    provider = _Provider()

    result = _capture(monkeypatch, tmp_path, provider)

    assert result["state"] == "stale_universe"
    assert provider.calls == []
    assert not (tmp_path / "auction_snapshot").exists()


def test_capture_rejects_previous_session_snapshot(tmp_path, monkeypatch):
    _seed(tmp_path)
    provider = _Provider(source_time="15:17:25.434")

    result = _capture(monkeypatch, tmp_path, provider)

    assert result["state"] == "stale_snapshot"
    assert result["source_time_match_ratio"] == 0.0
    assert not (tmp_path / "auction_snapshot").exists()


def test_capture_rejects_prev_close_mismatch(tmp_path, monkeypatch):
    _seed(tmp_path)
    provider = _Provider(prev_close=8.0)

    result = _capture(monkeypatch, tmp_path, provider)

    assert result["state"] == "stale_snapshot"
    assert result["prev_close_match_ratio"] == 0.0
    assert not (tmp_path / "auction_snapshot").exists()


def test_ready_partition_prevents_duplicate_network_capture(tmp_path, monkeypatch):
    _seed(tmp_path)
    provider = _Provider()
    assert _capture(monkeypatch, tmp_path, provider)["state"] == "ready"

    result = _capture(monkeypatch, tmp_path, provider)

    assert result["state"] == "already_captured"
    assert len(provider.calls) == 1


def test_outside_window_and_holiday_are_fail_closed(tmp_path, monkeypatch):
    _seed(tmp_path)
    provider = _Provider()
    monkeypatch.setattr(auction_snapshot.preferences, "get_realtime_data_provider", lambda: "mootdx")
    monkeypatch.setattr(auction_snapshot.custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(auction_snapshot.custom_sources, "get_provider", lambda name: provider)

    before = datetime(2026, 10, 9, 9, 24, 59, tzinfo=CN_TZ)
    assert auction_snapshot.capture_auction_snapshot(tmp_path, now=before)["state"] == "outside_window"
    monkeypatch.setattr(auction_snapshot.trading_day, "is_trading_day", lambda now: False)
    assert auction_snapshot.capture_auction_snapshot(tmp_path, now=NOW)["state"] == "market_closed"
    assert provider.calls == []


def test_reader_filters_gap_and_detects_incomplete_publication(tmp_path, monkeypatch):
    _seed(tmp_path)
    assert _capture(monkeypatch, tmp_path, _Provider())["state"] == "ready"

    payload = auction_snapshot.get_auction_snapshot(tmp_path, min_gap_pct=0.06)
    assert [row["symbol"] for row in payload["rows"]] == ["600000.SH"]

    metadata_path = (
        tmp_path / "auction_snapshot" / f"date={DAY.isoformat()}" / "metadata.json"
    )
    metadata = json.loads(metadata_path.read_text())
    metadata["capture_id"] = "different"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert auction_snapshot.get_auction_snapshot(tmp_path)["state"] == "incomplete"


def test_api_reads_requested_history_date(tmp_path, monkeypatch):
    _seed(tmp_path)
    assert _capture(monkeypatch, tmp_path, _Provider())["state"] == "ready"
    app = FastAPI()
    app.include_router(abnormal.router)
    app.state.repo = type("Repo", (), {"store": type("Store", (), {"data_dir": tmp_path})()})()

    response = TestClient(app).get(
        "/api/abnormal/auction",
        params={"date": DAY.isoformat(), "min_gap_pct": 0.06, "limit": 10},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["trade_date"] == DAY.isoformat()
    assert [row["symbol"] for row in payload["rows"]] == ["600000.SH"]


def test_scheduler_registers_retries_and_in_window_catchup(monkeypatch):
    calls: list[dict] = []

    class Scheduler:
        def add_job(self, func, **kwargs):
            calls.append({"func": func, **kwargs})

    repo = object()
    monkeypatch.setattr(daily_pipeline, "cn_now", lambda: NOW)
    daily_pipeline._register_auction_jobs(Scheduler(), repo)

    assert [call["id"] for call in calls] == [
        "auction_snapshot_8",
        "auction_snapshot_25",
        "auction_snapshot_45",
        "auction_snapshot_catchup",
    ]
    assert all(call["args"] == [repo] for call in calls)
