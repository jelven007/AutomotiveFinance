#!/usr/bin/env python
"""Analyze major market waves with strict point-in-time ST exclusions."""

# ruff: noqa: RUF001

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, date, datetime
from itertools import pairwise
from pathlib import Path

import numpy as np
import polars as pl

from app.services.fs_utils import atomic_write_parquet, atomic_write_text

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _daily_market(end: date) -> tuple[pl.DataFrame, dict]:
    bars = (
        pl.scan_parquet(
            str(DATA_DIR / "kline_daily_enriched" / "date=*" / "part.parquet"),
            hive_partitioning=False,
            missing_columns="insert",
            extra_columns="ignore",
        )
        .filter(pl.col("date") <= end)
        .select("symbol", "date", "close", "amount")
        .collect()
        .sort(["symbol", "date"])
        .with_columns(
            (pl.col("close") / pl.col("close").shift(1).over("symbol") - 1.0)
            .alias("return")
        )
    )
    history = (
        pl.read_parquet(DATA_DIR / "instrument_status" / "history.parquet")
        .select(
            "symbol",
            pl.col("valid_from").alias("_status_from"),
            pl.col("valid_to").alias("_status_to"),
            "is_risk_warning",
            "source",
        )
        .sort(["symbol", "_status_from"])
    )
    joined = bars.join_asof(
        history,
        left_on="date",
        right_on="_status_from",
        by="symbol",
        strategy="backward",
        check_sortedness=False,
    )
    status_known = (
        pl.col("_status_from").is_not_null()
        & (
            pl.col("_status_to").is_null()
            | (pl.col("date") < pl.col("_status_to"))
        )
    )
    joined = joined.with_columns(
        status_known.alias("status_known"),
        (
            ~status_known
            | pl.col("is_risk_warning").fill_null(True)
        ).alias("excluded"),
    )
    audit = {
        "bar_rows": joined.height,
        "symbols": joined["symbol"].n_unique(),
        "unknown_status_rows": joined.filter(~pl.col("status_known")).height,
        "excluded_rows": int(joined["excluded"].sum()),
        "excluded_symbols": joined.filter(pl.col("excluded"))["symbol"].n_unique(),
        "quarantine_rows": joined.filter(
            pl.col("source").str.ends_with("_f10_unknown_quarantine")
        ).height,
    }
    daily = (
        joined
        .filter(pl.col("return").is_finite())
        .group_by("date")
        .agg(
            pl.len().alias("return_rows"),
            pl.col("excluded").sum().alias("excluded_count"),
            (~pl.col("excluded")).sum().alias("eligible_count"),
            pl.col("return")
            .filter(~pl.col("excluded"))
            .clip(-0.20, 0.20)
            .mean()
            .alias("equal_weight_return"),
            pl.col("return")
            .filter(~pl.col("excluded"))
            .median()
            .alias("median_return"),
            (pl.col("return").filter(~pl.col("excluded")) > 0)
            .mean()
            .alias("up_ratio"),
            pl.col("amount")
            .filter(~pl.col("excluded"))
            .sum()
            .alias("amount"),
        )
        .sort("date")
        .with_columns(
            (
                (1.0 + pl.col("equal_weight_return")).cum_prod() * 1000.0
            ).alias("st_free_equal_weight_index")
        )
    )
    audit["daily_rows"] = daily.height
    audit["first_date"] = str(daily["date"].min())
    audit["last_date"] = str(daily["date"].max())
    return daily, audit


def _benchmark(end: date) -> pl.DataFrame:
    return (
        pl.scan_parquet(
            str(DATA_DIR / "kline_index_daily" / "date=*" / "part.parquet"),
            hive_partitioning=False,
            missing_columns="insert",
            extra_columns="ignore",
        )
        .filter(
            (pl.col("symbol") == "000001.SH")
            & (pl.col("date") <= end)
        )
        .select("date", "close")
        .sort("date")
        .collect()
    )


def confirmed_zigzag_pivots(values: np.ndarray, threshold: float) -> list[int]:
    """Return pivots confirmed by a reversal of at least ``threshold``."""
    if len(values) < 2:
        return [0] if len(values) else []
    high = low = 0
    extreme = 0
    direction = 0
    pivots: list[int] = []
    for index, value in enumerate(values):
        if direction == 0:
            if value > values[high]:
                high = index
            if value < values[low]:
                low = index
            if value <= values[high] * (1.0 - threshold):
                pivots.append(high)
                direction = -1
                extreme = index
            elif value >= values[low] * (1.0 + threshold):
                pivots.append(low)
                direction = 1
                extreme = index
        elif direction > 0:
            if value >= values[extreme]:
                extreme = index
            elif value <= values[extreme] * (1.0 - threshold):
                pivots.append(extreme)
                direction = -1
                extreme = index
        else:
            if value <= values[extreme]:
                extreme = index
            elif value >= values[extreme] * (1.0 + threshold):
                pivots.append(extreme)
                direction = 1
                extreme = index
    return pivots


def _segment_stats(
    benchmark: pl.DataFrame,
    daily: pl.DataFrame,
    start_id: int,
    end_id: int,
    *,
    confirmed: bool,
) -> dict:
    start = benchmark["date"][start_id]
    end = benchmark["date"][end_id]
    prices = benchmark["close"][start_id:end_id + 1].to_numpy().astype(float)
    start_close = float(prices[0])
    index_return = float(prices[-1] / start_close - 1.0)
    peaks = np.maximum.accumulate(prices)
    max_drawdown = float(np.min(prices / peaks - 1.0))
    market = daily.filter((pl.col("date") > start) & (pl.col("date") <= end))
    ew_return = (
        float((1.0 + market["equal_weight_return"]).product() - 1.0)
        if market.height
        else 0.0
    )
    return {
        "start": start,
        "end": end,
        "status": "confirmed" if confirmed else "ongoing",
        "direction": "up" if index_return >= 0 else "down",
        "trading_days": end_id - start_id,
        "calendar_days": (end - start).days,
        "index_start": round(start_close, 2),
        "index_end": round(float(prices[-1]), 2),
        "index_return": round(index_return, 6),
        "index_peak_return": round(float(np.max(prices) / start_close - 1.0), 6),
        "index_trough_return": round(float(np.min(prices) / start_close - 1.0), 6),
        "index_max_drawdown": round(max_drawdown, 6),
        "st_free_equal_weight_return": round(ew_return, 6),
        "average_up_ratio": round(float(market["up_ratio"].mean()), 6),
        "up_day_ratio": round(
            float((market["equal_weight_return"] > 0).mean()),
            6,
        ),
        "average_eligible_count": round(float(market["eligible_count"].mean()), 1),
        "average_excluded_count": round(float(market["excluded_count"].mean()), 1),
        "average_amount_trillion": round(float(market["amount"].mean()) / 1e12, 3),
    }


def analyze(end: date, threshold: float) -> tuple[pl.DataFrame, pl.DataFrame, dict]:
    daily, governance_audit = _daily_market(end)
    benchmark = _benchmark(end)
    values = benchmark["close"].to_numpy().astype(float)
    pivots = confirmed_zigzag_pivots(values, threshold)
    segments: list[dict] = []
    for start_id, end_id in pairwise(pivots):
        segments.append(
            _segment_stats(
                benchmark,
                daily,
                start_id,
                end_id,
                confirmed=True,
            )
        )
    if pivots and pivots[-1] < benchmark.height - 1:
        segments.append(
            _segment_stats(
                benchmark,
                daily,
                pivots[-1],
                benchmark.height - 1,
                confirmed=False,
            )
        )
    segment_frame = pl.DataFrame(segments).with_row_index("wave_id", offset=1)
    audit = {
        "generated_at": datetime.now(UTC).isoformat(),
        "effective_end": end.isoformat(),
        "benchmark": "000001.SH",
        "zigzag_reversal_threshold": threshold,
        "confirmed_pivot_count": len(pivots),
        "wave_count": segment_frame.height,
        "governance": governance_audit,
    }
    return daily, segment_frame, audit


def _write_report(
    output_dir: Path,
    waves: pl.DataFrame,
    audit: dict,
) -> None:
    rows = waves.to_dicts()

    def pct(value: float) -> str:
        return f"{float(value) * 100:.2f}%"

    lines = [
        "# 2016 年以来大级别行情波段（严格排除 ST/*ST）",
        "",
        "## 口径",
        "",
        f"- 区间：`2016-01-04` 至 `{audit['effective_end']}`。",
        "- 价格锚：上证指数；用反向波动达到 12% 确认前一高低点。",
        "- 市场宽度：对每个交易日按历史时点排除 ST、*ST 和状态未知标的，再计算全市场等权收益与上涨家数比例。",
        "- 个股日收益按前复权 close 计算，并裁剪至 ±20% 后求截面均值，降低新股无涨跌幅和极端值影响。",
        "- 最后一段若尚未发生 12% 反向波动，标记为“进行中”，不能视为已确认拐点。",
        "",
        "## 波段统计",
        "",
        "| # | 状态 | 方向 | 起点 | 终点 | 自然日 | 上证涨跌 | ST-free 等权 | 平均上涨家数 | 最大回撤 | 日均成交额 |",
        "|---:|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['wave_id']} | "
            f"{'已确认' if row['status'] == 'confirmed' else '进行中'} | "
            f"{'上涨' if row['direction'] == 'up' else '下跌'} | "
            f"{row['start']} | {row['end']} | {row['calendar_days']} | "
            f"{pct(row['index_return'])} | "
            f"{pct(row['st_free_equal_weight_return'])} | "
            f"{pct(row['average_up_ratio'])} | "
            f"{pct(row['index_max_drawdown'])} | "
            f"{row['average_amount_trillion']:.3f} 万亿元 |"
        )
    confirmed = [row for row in rows if row["status"] == "confirmed"]
    up = [row for row in confirmed if row["direction"] == "up"]
    down = [row for row in confirmed if row["direction"] == "down"]
    strongest = max(up, key=lambda row: row["index_return"])
    weakest = min(down, key=lambda row: row["index_return"])
    governance = audit["governance"]
    lines += [
        "",
        "## 摘要",
        "",
        f"- 最强已确认上涨波段：`{strongest['start']}` 至 `{strongest['end']}`，上证 `{pct(strongest['index_return'])}`，ST-free 等权 `{pct(strongest['st_free_equal_weight_return'])}`。",
        f"- 最深已确认下跌波段：`{weakest['start']}` 至 `{weakest['end']}`，上证 `{pct(weakest['index_return'])}`，ST-free 等权 `{pct(weakest['st_free_equal_weight_return'])}`。",
        f"- 状态区间覆盖未知行：`{governance['unknown_status_rows']}`；排除风险/隔离日线：`{governance['excluded_rows']:,}` 行，涉及 `{governance['excluded_symbols']}` 只股票。",
        "",
        "## 限制",
        "",
        "- 当前股票目录仍缺少历史已退市股票，治理解决了现有股票的历史 ST 时点问题，但不能消除幸存者偏差。",
        "- 上证指数偏大盘；ST-free 等权序列用于验证市场宽度，不是可直接交易指数。",
        "- ZigZag 是后验分段工具，只用于统计历史波段，不构成实时择时信号。",
    ]
    atomic_write_text(output_dir / "report.md", "\n".join(lines) + "\n")


def run(end: date, threshold: float) -> Path:
    output_dir = (
        DATA_DIR
        / "reports"
        / f"market-waves-20160101-{end:%Y%m%d}-st-governed"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    daily, waves, audit = analyze(end, threshold)
    atomic_write_parquet(daily, output_dir / "daily_market.parquet")
    atomic_write_parquet(waves, output_dir / "waves.parquet")
    with (output_dir / "waves.csv").open("w", encoding="utf-8-sig", newline="") as file:
        rows = waves.to_dicts()
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    atomic_write_text(
        output_dir / "audit.json",
        json.dumps(audit, ensure_ascii=False, indent=2, default=str),
    )
    _write_report(output_dir, waves, audit)
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="统计严格排除 ST 后的大级别行情波段")
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--threshold", type=float, default=0.12)
    args = parser.parse_args()
    output = run(args.end, args.threshold)
    print(output)


if __name__ == "__main__":
    main()
