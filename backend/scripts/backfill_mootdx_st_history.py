#!/usr/bin/env python
"""Backfill strict point-in-time ST/*ST history from mootdx TDX F10.

Run from ``backend/``:

    .venv/bin/python -m scripts.backfill_mootdx_st_history --end 2026-09-30
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.instrument_history import history_path, load_instrument_history
from app.plugins.mootdx.client import MootdxClient
from app.services.fs_utils import atomic_write_parquet, atomic_write_text
from app.services.st_history_backfill import (
    RAW_SCHEMA,
    build_governed_history,
    events_frame,
    parse_special_treatment_events,
)

logger = logging.getLogger(__name__)
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
GOVERNANCE_DIR = DATA_DIR / "governance" / "st_history"
RAW_PATH = GOVERNANCE_DIR / "mootdx_f10_latest.parquet"
EVENTS_PATH = GOVERNANCE_DIR / "events.parquet"
GOVERNED_HISTORY_PATH = GOVERNANCE_DIR / "history_governed.parquet"
MANIFEST_PATH = GOVERNANCE_DIR / "manifest.json"
REPORT_PATH = GOVERNANCE_DIR / "report.md"
_thread_local = threading.local()
_clients: list[MootdxClient] = []
_clients_lock = threading.Lock()


def _client() -> MootdxClient:
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = MootdxClient()
        client.__enter__()
        _thread_local.client = client
        with _clients_lock:
            _clients.append(client)
    return client


def _fetch_one(symbol: str, retries: int) -> dict:
    last_error: Exception | None = None
    for _attempt in range(max(1, retries)):
        try:
            text = _client().company_info(symbol, "最新提示")
            if not text:
                raise ValueError("F10 最新提示为空")
            if "【特别处理】" not in text:
                raise ValueError("F10 最新提示缺少特别处理栏目")
            return {
                "symbol": symbol,
                "text": text,
                "fetched_at": datetime.now(UTC).isoformat(),
                "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "error": None,
            }
        except Exception as exc:
            last_error = exc
            client = getattr(_thread_local, "client", None)
            if client is not None:
                client.close()
                _thread_local.client = None
    return {
        "symbol": symbol,
        "text": None,
        "fetched_at": datetime.now(UTC).isoformat(),
        "content_sha256": None,
        "error": str(last_error or "unknown error"),
    }


def _close_clients() -> None:
    with _clients_lock:
        clients = list(_clients)
        _clients.clear()
    for client in clients:
        client.close()


def _raw_frame(records: dict[str, dict]) -> pl.DataFrame:
    return pl.DataFrame(
        [records[symbol] for symbol in sorted(records)],
        schema=RAW_SCHEMA,
    )


def _checkpoint(records: dict[str, dict]) -> None:
    GOVERNANCE_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(_raw_frame(records), RAW_PATH)


def _coverage(end: date) -> pl.DataFrame:
    return (
        pl.scan_parquet(
            str(DATA_DIR / "kline_daily" / "date=*" / "part.parquet"),
            hive_partitioning=False,
            missing_columns="insert",
            extra_columns="ignore",
        )
        .filter(pl.col("date") <= end)
        .group_by("symbol")
        .agg(
            pl.col("date").min().alias("first_date"),
            pl.col("date").max().alias("last_date"),
            pl.len().alias("bars"),
        )
        .sort("symbol")
        .collect(engine="streaming")
    )


def run(*, end: date, workers: int, retries: int, checkpoint_every: int) -> dict:
    GOVERNANCE_DIR.mkdir(parents=True, exist_ok=True)
    coverage = _coverage(end)
    symbols = coverage["symbol"].cast(pl.String).to_list()
    instruments = pl.read_parquet(DATA_DIR / "instruments" / "instruments.parquet")
    instrument_symbols = set(instruments["symbol"].cast(pl.String).to_list())
    missing_instruments = set(symbols) - instrument_symbols
    if missing_instruments:
        logger.warning(
            "%d 个行情标的缺少 instruments 记录, 将按未知状态全历史隔离",
            len(missing_instruments),
        )
        instruments = pl.concat(
            [
                instruments,
                pl.DataFrame(
                    {
                        "symbol": sorted(missing_instruments),
                        "name": [""] * len(missing_instruments),
                    }
                ),
            ],
            how="diagonal_relaxed",
        )

    records: dict[str, dict] = {}
    if RAW_PATH.exists():
        records = {
            str(row["symbol"]): row
            for row in pl.read_parquet(RAW_PATH).iter_rows(named=True)
            if str(row["symbol"]) in symbols
        }
    for symbol in missing_instruments:
        records[symbol] = {
            "symbol": symbol,
            "text": None,
            "fetched_at": datetime.now(UTC).isoformat(),
            "content_sha256": None,
            "error": "missing current instruments record",
        }
    pending = [
        symbol
        for symbol in symbols
        if symbol not in missing_instruments
        and (
            symbol not in records
            or records[symbol].get("error")
            or not records[symbol].get("text")
        )
    ]
    logger.info(
        "ST F10 governance: symbols=%d cached=%d pending=%d workers=%d",
        len(symbols),
        len(symbols) - len(pending),
        len(pending),
        workers,
    )

    completed = 0
    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {
                executor.submit(_fetch_one, symbol, retries): symbol
                for symbol in pending
            }
            for future in as_completed(futures):
                row = future.result()
                records[row["symbol"]] = row
                completed += 1
                if completed % max(1, checkpoint_every) == 0:
                    _checkpoint(records)
                    errors = sum(bool(item.get("error")) for item in records.values())
                    logger.info("F10 progress: %d/%d, errors=%d", completed, len(pending), errors)
    finally:
        _close_clients()
    _checkpoint(records)

    event_records: list[dict] = []
    quarantined: set[str] = set()
    parse_errors: dict[str, str] = {}
    successful: set[str] = set()
    for symbol in symbols:
        row = records.get(symbol)
        if row is None or row.get("error") or not row.get("text"):
            quarantined.add(symbol)
            continue
        try:
            event_records.extend(parse_special_treatment_events(symbol, row["text"]))
            successful.add(symbol)
        except Exception as exc:
            parse_errors[symbol] = str(exc)
            quarantined.add(symbol)

    events = events_frame(event_records)
    observed_at = datetime.now(UTC)
    governed = build_governed_history(
        instruments,
        coverage,
        events,
        successful_symbols=successful,
        quarantined_symbols=quarantined,
        as_of=max(end, date.today()),
        available_at=observed_at,
    )

    existing = load_instrument_history(DATA_DIR)
    covered = set(symbols)
    untouched = (
        existing.filter(~pl.col("symbol").is_in(sorted(covered)))
        if not existing.is_empty()
        else existing
    )
    published = (
        governed
        if untouched.is_empty()
        else pl.concat([governed, untouched], how="vertical_relaxed")
    ).sort(["symbol", "valid_from"])

    atomic_write_parquet(events, EVENTS_PATH)
    atomic_write_parquet(published, GOVERNED_HISTORY_PATH)
    target = history_path(DATA_DIR)
    if target.exists():
        backup = GOVERNANCE_DIR / f"history-before-{observed_at:%Y%m%dT%H%M%SZ}.parquet"
        shutil.copy2(target, backup)
    atomic_write_parquet(published, target)

    fetch_errors = {
        symbol: str(row.get("error"))
        for symbol, row in records.items()
        if row.get("error")
    }
    manifest = {
        "schema_version": 1,
        "generated_at": observed_at.isoformat(),
        "provider": "mootdx_f10",
        "effective_end": end.isoformat(),
        "symbols_with_daily_data": len(symbols),
        "missing_instrument_count": len(missing_instruments),
        "missing_instruments": sorted(missing_instruments),
        "fetched_successfully": len(successful),
        "fetch_error_count": len(fetch_errors),
        "parse_error_count": len(parse_errors),
        "quarantined_symbol_count": len(quarantined),
        "event_count": events.height,
        "risk_event_count": events.filter(
            pl.col("is_risk_warning_after").is_not_null()
        ).height,
        "governed_interval_count": governed.height,
        "published_interval_count": published.height,
        "risk_symbols": governed.filter(pl.col("is_risk_warning"))
        ["symbol"].n_unique(),
        "treatment_types": (
            events.group_by("treatment_type")
            .len()
            .sort("len", descending=True)
            .to_dicts()
        ),
        "fetch_errors": fetch_errors,
        "parse_errors": parse_errors,
        "files": {
            "raw": str(RAW_PATH),
            "events": str(EVENTS_PATH),
            "governed_history": str(GOVERNED_HISTORY_PATH),
            "published_history": str(target),
            "report": str(REPORT_PATH),
        },
        "policy": "fetch/parse unknown => risk-warning quarantine for full local history",
    }
    report_lines = [
        "# ST/*ST 历史状态治理报告",
        "",
        f"- 行情标的: `{len(symbols):,}`",
        f"- F10 成功解析: `{len(successful):,}`",
        f"- 特别处理事件: `{events.height:,}`",
        f"- 历史风险标的: `{manifest['risk_symbols']:,}`",
        f"- 严格隔离标的: `{len(quarantined):,}`",
        f"- 发布状态区间: `{published.height:,}`",
        "",
        "## 隔离原因",
        "",
    ]
    for symbol in sorted(quarantined):
        reason = fetch_errors.get(symbol) or parse_errors.get(symbol) or "unknown"
        report_lines.append(f"- `{symbol}`: {reason}")
    report_lines += [
        "",
        "## 规则",
        "",
        "- 数据源为 mootdx 通达信 F10 最新提示中的特别处理历史。",
        "- 按实施日期构造 SCD2 区间, valid_to 为开区间。",
        "- 抓取失败、解析失败、当前目录缺失一律全历史隔离。",
        "- 只治理风险警示时点; 历史简称仍使用当前展示名称。",
    ]
    atomic_write_text(REPORT_PATH, "\n".join(report_lines) + "\n")
    atomic_write_text(MANIFEST_PATH, json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 mootdx F10 历史 ST/*ST 时点区间")
    parser.add_argument("--end", type=date.fromisoformat, required=True, help="治理行情截止日")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--checkpoint-every", type=int, default=100)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    manifest = run(
        end=args.end,
        workers=args.workers,
        retries=args.retries,
        checkpoint_every=args.checkpoint_every,
    )
    logger.info("ST governance complete: %s", json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
