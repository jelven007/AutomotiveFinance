"""Persist verified full-market 09:25 call-auction snapshots.

History starts when this collector is enabled. Daily/minute bars are never used
to synthesize missing auction observations.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import uuid
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.data_providers import custom as custom_sources
from app.data_providers.instrument_status import (
    is_delisted_name,
    normalize_instrument_name,
)
from app.market_time import CN_TZ, cn_now
from app.services import alert_store, preferences, trading_day, webhook_adapter
from app.services.fs_utils import atomic_write_parquet, atomic_write_text

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^date=(\d{4}-\d{2}-\d{2})$")
_CAPTURE_START = dt_time(9, 25, 5)
_CAPTURE_END = dt_time(9, 29, 30)
_MIN_COVERAGE_RATIO = 0.95
_MIN_PREV_CLOSE_MATCH_RATIO = 0.80
_LOCK = threading.Lock()
_FAILURE_ALERT_LOCK = threading.Lock()
_NON_FAILURE_STATES = {"ready", "already_captured", "market_closed"}

_SCHEMA = {
    "capture_id": pl.String,
    "trade_date": pl.Date,
    "captured_at": pl.Datetime("us"),
    "provider": pl.String,
    "symbol": pl.String,
    "name": pl.String,
    "exchange": pl.String,
    "auction_price": pl.Float64,
    "indicative_price": pl.Float64,
    "prev_close": pl.Float64,
    "auction_change_pct": pl.Float64,
    "auction_volume": pl.Float64,
    "auction_amount": pl.Float64,
    "bid1": pl.Float64,
    "bid1_volume": pl.Float64,
    "ask1": pl.Float64,
    "ask1_volume": pl.Float64,
    "order_imbalance": pl.Float64,
    "source_time": pl.String,
    "received_at_ms": pl.Int64,
    "matched": pl.Boolean,
}


def _root(data_dir: Path) -> Path:
    return data_dir / "auction_snapshot"


def _partition(data_dir: Path, day: date) -> Path:
    return _root(data_dir) / f"date={day.isoformat()}"


def _metadata_path(data_dir: Path, day: date) -> Path:
    return _partition(data_dir, day) / "metadata.json"


def _data_path(data_dir: Path, day: date) -> Path:
    return _partition(data_dir, day) / "part.parquet"


def _positive(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _nonnegative(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _active_universe(data_dir: Path, day: date) -> tuple[pl.DataFrame, dict]:
    path = data_dir / "instruments" / "instruments.parquet"
    if not path.exists():
        return pl.DataFrame(), {"state": "stale_universe", "message": "标的目录不存在"}
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        return pl.DataFrame(), {"state": "stale_universe", "message": f"标的目录读取失败: {exc}"}
    required = {"symbol", "name", "exchange", "as_of"}
    if frame.is_empty() or not required.issubset(frame.columns):
        return pl.DataFrame(), {"state": "stale_universe", "message": "标的目录字段不完整"}

    frame = frame.with_columns(
        pl.col("as_of").cast(pl.Date, strict=False),
        pl.col("symbol").cast(pl.String),
        pl.col("name").cast(pl.String, strict=False).fill_null("").map_elements(
            normalize_instrument_name,
            return_dtype=pl.String,
        ),
        pl.col("exchange").cast(pl.String),
    )
    min_as_of = frame["as_of"].min()
    max_as_of = frame["as_of"].max()
    if min_as_of != day or max_as_of != day:
        return pl.DataFrame(), {
            "state": "stale_universe",
            "message": f"标的目录日期为 {min_as_of}..{max_as_of}, 需要 {day.isoformat()}",
            "universe_as_of": max_as_of.isoformat() if max_as_of else None,
        }

    eligible = frame.filter(pl.col("exchange").is_in(["SH", "SZ"]))
    if "type" in eligible.columns:
        eligible = eligible.filter(pl.col("type").cast(pl.String) == "stock")
    before = eligible.height
    eligible = eligible.filter(
        ~pl.col("name").map_elements(is_delisted_name, return_dtype=pl.Boolean)
    )
    eligible = (
        eligible.select("symbol", "name", "exchange", "as_of")
        .drop_nulls("symbol")
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )
    if eligible.is_empty():
        return eligible, {"state": "stale_universe", "message": "当日在市沪深股票目录为空"}
    return eligible, {
        "universe_as_of": day.isoformat(),
        "universe_count": eligible.height,
        "excluded_delisted_count": before - eligible.height,
    }


def _latest_prior_closes(data_dir: Path, day: date) -> tuple[date | None, dict[str, float]]:
    root = data_dir / "kline_daily"
    candidates: list[tuple[date, Path]] = []
    try:
        for path in root.iterdir():
            match = _DATE_RE.fullmatch(path.name)
            if match and path.is_dir():
                partition_day = date.fromisoformat(match.group(1))
                if partition_day < day:
                    candidates.append((partition_day, path))
    except OSError:
        return None, {}
    if not candidates:
        return None, {}
    latest_day, latest_path = max(candidates, key=lambda item: item[0])
    try:
        files = sorted(latest_path.glob("*.parquet"))
        frame = pl.concat(
            [pl.read_parquet(path, columns=["symbol", "close"]) for path in files],
            how="diagonal_relaxed",
        )
        frame = frame.drop_nulls(["symbol", "close"]).unique(subset=["symbol"], keep="last")
        return latest_day, {
            str(symbol): float(close)
            for symbol, close in frame.select("symbol", "close").iter_rows()
            if _positive(close) is not None
        }
    except (OSError, pl.exceptions.PolarsError):
        return latest_day, {}


def _source_time_in_window(value: object) -> bool:
    text = str(value or "").strip()
    try:
        # TDX hours are not zero-padded: slicing "9:25:08.125" at 8 leaves
        # a trailing dot. Parse the whole value, including fractional seconds.
        fmt = "%H:%M:%S.%f" if "." in text else "%H:%M:%S"
        parsed = datetime.strptime(text, fmt).time()
    except ValueError:
        return False
    return dt_time(9, 25) <= parsed < dt_time(9, 30)


def _load_metadata(data_dir: Path, day: date) -> dict | None:
    try:
        return json.loads(_metadata_path(data_dir, day).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ready_partition(data_dir: Path, day: date) -> tuple[dict, pl.DataFrame] | None:
    metadata = _load_metadata(data_dir, day)
    path = _data_path(data_dir, day)
    if not metadata or not path.exists():
        return None
    try:
        frame = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError):
        return None
    if (
        frame.is_empty()
        or "capture_id" not in frame.columns
        or frame["capture_id"].n_unique() != 1
        or frame["capture_id"][0] != metadata.get("capture_id")
    ):
        return None
    return metadata, frame


def capture_auction_snapshot(
    data_dir: Path,
    *,
    now: datetime | None = None,
) -> dict:
    """Capture one verified full-market snapshot during the 09:25-09:30 window."""
    now = (now or cn_now()).astimezone(CN_TZ)
    day = now.date()
    if now.weekday() >= 5 or not (_CAPTURE_START <= now.time() <= _CAPTURE_END):
        return {"state": "outside_window", "trade_date": day.isoformat()}
    if trading_day.is_trading_day(now) is False:
        return {"state": "market_closed", "trade_date": day.isoformat()}

    with _LOCK:
        ready = _ready_partition(data_dir, day)
        if ready is not None:
            return {**ready[0], "state": "already_captured"}

        universe, universe_meta = _active_universe(data_dir, day)
        if universe.is_empty():
            return {**universe_meta, "trade_date": day.isoformat()}

        provider_name = preferences.get_realtime_data_provider()
        if not custom_sources.is_custom_provider(provider_name):
            return {
                "state": "source_unavailable",
                "trade_date": day.isoformat(),
                "provider": provider_name,
                "message": "9:25 全市场竞价采集要求实时行情路由提供竞价快照能力",
            }
        provider = custom_sources.get_provider(provider_name)
        fetch = getattr(provider, "get_auction_snapshot", None)
        if not callable(fetch):
            return {
                "state": "source_unavailable",
                "trade_date": day.isoformat(),
                "provider": provider_name,
                "message": f"当前 {provider_name} Provider 未提供竞价快照能力",
            }

        symbols = universe["symbol"].to_list()
        try:
            raw = fetch(symbols)
        except Exception as exc:
            logger.warning("09:25 竞价快照拉取失败: %s", exc)
            return {
                "state": "fetch_failed",
                "trade_date": day.isoformat(),
                "provider": provider_name,
                "message": str(exc),
            }
        if isinstance(raw, pl.DataFrame):
            raw = raw.to_dicts()
        symbol_set = set(symbols)
        rows_by_symbol = {
            str(row.get("symbol")): row
            for row in (raw or [])
            if isinstance(row, dict) and row.get("symbol") in symbol_set
        }
        quote_count = len(rows_by_symbol)
        coverage_ratio = quote_count / universe.height
        if coverage_ratio < _MIN_COVERAGE_RATIO:
            return {
                "state": "incomplete_snapshot",
                "trade_date": day.isoformat(),
                "provider": provider_name,
                **universe_meta,
                "quote_count": quote_count,
                "coverage_ratio": coverage_ratio,
            }

        # TDX servertime is each symbol's last quote event, not this batch's
        # fetch time. Keep its auction-window ratio as diagnostics only.
        source_time_match_ratio = (
            sum(_source_time_in_window(row.get("source_time")) for row in rows_by_symbol.values())
            / quote_count
        )
        prior_day, prior_closes = _latest_prior_closes(data_dir, day)
        comparable = 0
        close_matches = 0
        for symbol, row in rows_by_symbol.items():
            expected = prior_closes.get(symbol)
            received = _positive(row.get("prev_close"))
            if expected is None or received is None:
                continue
            comparable += 1
            if abs(received - expected) <= 0.011:
                close_matches += 1
        minimum_comparisons = min(100, universe.height)
        prev_close_match_ratio = close_matches / comparable if comparable else 0.0
        if (
            comparable < minimum_comparisons
            or prev_close_match_ratio < _MIN_PREV_CLOSE_MATCH_RATIO
        ):
            source_time_samples = sorted({
                str(row.get("source_time") or "").strip()
                for row in rows_by_symbol.values()
                if row.get("source_time")
            })[:5]
            reasons: list[str] = []
            if comparable < minimum_comparisons:
                reasons.append(f"昨收可比样本不足: {comparable}/{minimum_comparisons}")
            if prev_close_match_ratio < _MIN_PREV_CLOSE_MATCH_RATIO:
                reasons.append(
                    f"昨收匹配率不足: {prev_close_match_ratio:.1%}, "
                    f"要求至少 {_MIN_PREV_CLOSE_MATCH_RATIO:.0%}"
                )
            return {
                "state": "stale_snapshot",
                "message": "; ".join(reasons),
                "trade_date": day.isoformat(),
                "provider": provider_name,
                **universe_meta,
                "quote_count": quote_count,
                "coverage_ratio": coverage_ratio,
                "source_time_match_ratio": source_time_match_ratio,
                "source_time_samples": source_time_samples,
                "prev_close_match_ratio": prev_close_match_ratio,
                "prev_close_sample_count": comparable,
                "latest_prior_daily_date": prior_day.isoformat() if prior_day else None,
            }

        capture_id = uuid.uuid4().hex
        captured_at = now.replace(tzinfo=None)
        names = dict(universe.select("symbol", "name").iter_rows())
        exchanges = dict(universe.select("symbol", "exchange").iter_rows())
        output: list[dict] = []
        for symbol in symbols:
            quote = rows_by_symbol.get(symbol)
            if quote is None:
                continue
            auction_price = _positive(quote.get("open"))
            indicative_price = _positive(quote.get("last_price"))
            prev_close = _positive(quote.get("prev_close"))
            volume = _nonnegative(quote.get("volume"))
            amount = _nonnegative(quote.get("amount"))
            bid_volume = _nonnegative(quote.get("bid1_volume"))
            ask_volume = _nonnegative(quote.get("ask1_volume"))
            imbalance = None
            if bid_volume is not None and ask_volume is not None and bid_volume + ask_volume > 0:
                imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
            output.append({
                "capture_id": capture_id,
                "trade_date": day,
                "captured_at": captured_at,
                "provider": provider_name,
                "symbol": symbol,
                "name": names.get(symbol),
                "exchange": exchanges.get(symbol),
                "auction_price": auction_price,
                "indicative_price": indicative_price,
                "prev_close": prev_close,
                "auction_change_pct": (
                    auction_price / prev_close - 1.0
                    if auction_price is not None and prev_close is not None
                    else None
                ),
                "auction_volume": volume,
                "auction_amount": amount,
                "bid1": _positive(quote.get("bid1")),
                "bid1_volume": bid_volume,
                "ask1": _positive(quote.get("ask1")),
                "ask1_volume": ask_volume,
                "order_imbalance": imbalance,
                "source_time": str(quote.get("source_time") or "") or None,
                "received_at_ms": int(quote["timestamp"]) if quote.get("timestamp") else None,
                "matched": auction_price is not None and volume is not None and volume > 0,
            })
        frame = pl.from_dicts(output, schema=_SCHEMA, strict=False).sort("symbol")
        matched_count = int(frame["matched"].sum())
        metadata = {
            "schema_version": 1,
            "state": "ready",
            "capture_id": capture_id,
            "trade_date": day.isoformat(),
            "captured_at": captured_at.isoformat(timespec="seconds"),
            "provider": provider_name,
            **universe_meta,
            "quote_count": quote_count,
            "matched_count": matched_count,
            "coverage_ratio": coverage_ratio,
            "source_time_match_ratio": source_time_match_ratio,
            "prev_close_match_ratio": prev_close_match_ratio,
            "prev_close_sample_count": comparable,
            "latest_prior_daily_date": prior_day.isoformat() if prior_day else None,
        }
        partition = _partition(data_dir, day)
        partition.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(frame, _data_path(data_dir, day))
        atomic_write_text(
            _metadata_path(data_dir, day),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
        from app.services.data_release import publish_data_release

        release = publish_data_release(
            data_dir,
            reason=f"auction_snapshot:{day.isoformat()}",
        )
        metadata["data_release_id"] = release["release_id"]
        logger.info(
            "09:25 竞价快照完成: %s, universe=%d quotes=%d matched=%d",
            day, universe.height, quote_count, matched_count,
        )
        return metadata


def notify_capture_failure(
    data_dir: Path,
    result: dict,
    *,
    quote_service=None,
) -> bool:
    """Persist and broadcast one final auction-capture failure per trade date."""
    state = str(result.get("state") or "unknown")
    if state in _NON_FAILURE_STATES:
        return False

    trade_date = str(result.get("trade_date") or cn_now().date().isoformat())
    with _FAILURE_ALERT_LOCK:
        recent = alert_store.list_recent(
            data_dir,
            days=alert_store.MAX_DAYS,
            source="market",
            type="auction_capture_failed",
        )
        if any(event.get("trade_date") == trade_date for event in recent):
            logger.info("09:25 auction failure alert already sent for %s", trade_date)
            return False

        reason = str(result.get("message") or state)
        message = f"09:25 竞价快照采集失败 ({trade_date}): {reason}"
        diagnostic_keys = (
            "provider",
            "universe_as_of",
            "universe_count",
            "quote_count",
            "coverage_ratio",
            "source_time_match_ratio",
            "source_time_samples",
            "prev_close_match_ratio",
            "prev_close_sample_count",
            "latest_prior_daily_date",
        )
        diagnostics = {
            key: result[key]
            for key in diagnostic_keys
            if result.get(key) is not None
        }
        event = {
            "ts": int(cn_now().timestamp() * 1000),
            "rule_id": "system.auction_snapshot",
            "rule_name": "竞价采集",
            "source": "market",
            "type": "auction_capture_failed",
            "symbol": "",
            "name": "",
            "message": message,
            "price": None,
            "change_pct": None,
            "signals": [],
            "severity": "critical",
            "trade_date": trade_date,
            "failure_state": state,
            "diagnostics": diagnostics,
        }
        alert_store.append(data_dir, event)

    if quote_service is not None:
        try:
            quote_service.push_alerts([event])
        except Exception:
            logger.warning("09:25 auction failure SSE broadcast failed", exc_info=True)

    try:
        webhook_url = preferences.get_feishu_webhook_url()
        if webhook_url:
            webhook_secret = preferences.get_feishu_webhook_secret()
            body = f"{message}\n失败状态: {state}"
            if diagnostics:
                body += "\n诊断信息: " + json.dumps(
                    diagnostics,
                    ensure_ascii=False,
                    sort_keys=True,
                )
            if not webhook_adapter.send_feishu(
                webhook_url,
                "竞价采集失败",
                body,
                webhook_secret,
            ):
                logger.warning("09:25 auction failure Feishu notification failed")
    except Exception:
        logger.warning("09:25 auction failure Feishu notification error", exc_info=True)

    return True


def _partition_dates(data_dir: Path) -> list[date]:
    dates: list[date] = []
    try:
        for path in _root(data_dir).iterdir():
            match = _DATE_RE.fullmatch(path.name)
            if match and path.is_dir():
                dates.append(date.fromisoformat(match.group(1)))
    except OSError:
        pass
    return sorted(dates)


def _available_dates(data_dir: Path) -> list[date]:
    return [
        day for day in _partition_dates(data_dir)
        if _metadata_path(data_dir, day).exists() and _data_path(data_dir, day).exists()
    ]


def get_auction_snapshot(
    data_dir: Path,
    target: date | None = None,
    *,
    min_gap_pct: float | None = None,
    limit: int = 300,
) -> dict:
    """Read one immutable auction snapshot for API/UI consumption."""
    dates = _available_dates(data_dir)
    available = [value.isoformat() for value in dates]
    if target is None:
        partitions = _partition_dates(data_dir)
        target = dates[-1] if dates else (partitions[-1] if partitions else None)
    if target is None:
        return {"state": "not_collected", "available_dates": []}
    ready = _ready_partition(data_dir, target)
    if ready is None:
        path = _partition(data_dir, target)
        state = "incomplete" if path.exists() else "not_collected"
        return {
            "state": state,
            "trade_date": target.isoformat(),
            "available_dates": available,
        }
    metadata, frame = ready
    if min_gap_pct is not None:
        frame = frame.filter(pl.col("auction_change_pct") >= min_gap_pct)
    frame = frame.sort("auction_change_pct", descending=True, nulls_last=True)
    total = frame.height
    frame = frame.head(max(1, min(limit, 2000)))
    rows: list[dict] = []
    for row in frame.to_dicts():
        row["trade_date"] = row["trade_date"].isoformat()
        row["captured_at"] = row["captured_at"].isoformat(timespec="seconds")
        rows.append(row)
    return {
        **metadata,
        "state": "ready",
        "available_dates": available,
        "filtered_count": total,
        "rows": rows,
    }
