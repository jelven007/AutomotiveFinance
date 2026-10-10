#!/usr/bin/env python3
"""Run every public builtin stock strategy against point-in-time ST history."""
# ruff: noqa: E402, RUF001

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.backtest.engine import BacktestEngine
from app.backtest.strategy import (
    BacktestResultPolicy,
    StrategyBacktestConfig,
    StrategyBacktestService,
)
from app.factors.store import load_into_registry
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyEngine
from app.tickflow.repository import DataStore, KlineRepository

REQUESTED_START = date(2016, 1, 1)
EFFECTIVE_END = date(2026, 9, 30)
INITIAL_CAPITAL = 1_000_000.0
MAX_POSITIONS = 10
COMMISSION_PCT = 0.0002
STAMP_TAX_PCT = 0.001
SLIPPAGE_BPS = 5.0

SUMMARY_FIELDS = [
    "rank",
    "strategy_id",
    "strategy_name",
    "total_return",
    "annual_return",
    "max_drawdown",
    "sharpe",
    "sortino",
    "calmar",
    "win_rate",
    "avg_win_loss_ratio",
    "n_trades",
    "avg_pnl",
    "median_pnl",
    "avg_win",
    "avg_loss",
    "best_trade",
    "worst_trade",
    "avg_holding_days",
    "avg_exposure",
    "max_exposure",
    "final_equity",
    "strategy_matches",
    "elapsed_ms",
    "error",
]


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime, Path)):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_output(*args: str) -> str:
    return subprocess.check_output(
        args,
        cwd=REPO_DIR,
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def _git_state() -> tuple[str, str]:
    head = _command_output("git", "rev-parse", "HEAD")
    diff = subprocess.check_output(["git", "diff", "--binary"], cwd=REPO_DIR)
    return head, hashlib.sha256(diff).hexdigest()


def _strategy_dirs(data_dir: Path) -> list[Path]:
    return [
        BACKEND_DIR / "app" / "strategy" / "builtin",
        data_dir / "strategies" / "custom",
        data_dir / "strategies" / "ai",
        data_dir / "strategies" / "composite",
    ]


def _load_governance(data_dir: Path) -> dict[str, Any]:
    manifest_path = data_dir / "governance" / "st_history" / "manifest.json"
    history_path = data_dir / "instrument_status" / "history.parquet"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    history = pl.read_parquet(history_path)
    source_counts = {
        str(row["source"]): int(row["len"])
        for row in history.group_by("source").len().iter_rows(named=True)
    }
    risk = history.filter(pl.col("is_risk_warning"))
    return {
        "manifest_path": str(manifest_path),
        "manifest_sha256": _sha256_file(manifest_path),
        "history_path": str(history_path),
        "history_sha256": _sha256_file(history_path),
        "effective_end": manifest["effective_end"],
        "symbols_with_daily_data": int(manifest["symbols_with_daily_data"]),
        "fetched_successfully": int(manifest["fetched_successfully"]),
        "fetch_error_count": int(manifest["fetch_error_count"]),
        "parse_error_count": int(manifest["parse_error_count"]),
        "quarantined_symbol_count": int(manifest["quarantined_symbol_count"]),
        "event_count": int(manifest["event_count"]),
        "governed_interval_count": int(manifest["governed_interval_count"]),
        "manifest_published_interval_count": int(manifest["published_interval_count"]),
        "published_rows_current": int(history.height),
        "published_symbols_current": int(history.get_column("symbol").n_unique()),
        "published_valid_from_min": str(history.get_column("valid_from").min()),
        "published_valid_from_max": str(history.get_column("valid_from").max()),
        "risk_rows_current": int(risk.height),
        "risk_symbols_current": int(risk.get_column("symbol").n_unique()),
        "source_counts": source_counts,
        "policy": manifest["policy"],
    }


def _build_run_spec(data_dir: Path, strategy_count: int) -> dict[str, Any]:
    git_head, diff_hash = _git_state()
    governance = _load_governance(data_dir)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "requested_start": str(REQUESTED_START),
        "requested_end": str(EFFECTIVE_END),
        "actual_start": "2016-01-04",
        "effective_end": str(EFFECTIVE_END),
        "asset_type": "stock",
        "universe": "all_local_stock_symbols",
        "strategy_scope": "public_builtin",
        "strategy_count": strategy_count,
        "mode": "position",
        "entry_fill": "open_t+1",
        "exit_fill": "open_t+1",
        "commission_pct": COMMISSION_PCT,
        "stamp_tax_pct": STAMP_TAX_PCT,
        "slippage_bps": SLIPPAGE_BPS,
        "initial_capital": INITIAL_CAPITAL,
        "max_positions": MAX_POSITIONS,
        "max_exposure_pct": 1.0,
        "position_sizing": "equal",
        "git_head": git_head,
        "worktree_diff_sha256": diff_hash,
        "st_governance": governance,
    }


def _make_config(strategy_id: str) -> StrategyBacktestConfig:
    return StrategyBacktestConfig(
        strategy_id=strategy_id,
        symbols=None,
        start=REQUESTED_START,
        end=EFFECTIVE_END,
        matching="open_t+1",
        entry_fill="open_t+1",
        exit_fill="open_t+1",
        fees_pct=COMMISSION_PCT,
        commission_pct=COMMISSION_PCT,
        stamp_tax_pct=STAMP_TAX_PCT,
        slippage_bps=SLIPPAGE_BPS,
        max_positions=MAX_POSITIONS,
        max_exposure_pct=1.0,
        initial_capital=INITIAL_CAPITAL,
        position_sizing="equal",
        mode="position",
        asset_type="stock",
        holding_days=5,
        minute_fill=False,
        regime_filter=None,
    )


def _open_service(
    data_dir: Path,
) -> tuple[StrategyBacktestService, StrategyEngine, DataStore]:
    load_into_registry(data_dir)
    store = DataStore(data_dir)
    repo = KlineRepository(store)
    strategy_engine = StrategyEngine(
        strategy_dirs=_strategy_dirs(data_dir),
        override_loader=lambda strategy_id: strategy_config.load_override(
            data_dir, strategy_id
        ),
    )
    service = StrategyBacktestService(BacktestEngine(repo), strategy_engine)
    return service, strategy_engine, store


def _slim_result(strategy: dict[str, Any], raw_result: Any) -> dict[str, Any]:
    result = asdict(raw_result)
    return {
        "strategy_id": strategy["id"],
        "strategy_name": strategy.get("name") or strategy["id"],
        "config": result["config"],
        "stats": result["stats"],
        "strategy_info": result["strategy_info"],
        "error": result["error"],
        "elapsed_ms": result["elapsed_ms"],
        "provenance": result["provenance"],
    }


def _run_strategies(
    data_dir: Path,
    output_dir: Path,
) -> tuple[dict[str, Any], StrategyBacktestService, DataStore]:
    service, strategy_engine, store = _open_service(data_dir)
    strategies = sorted(
        (
            item
            for item in strategy_engine.list_strategies()
            if item.get("source") == "builtin"
        ),
        key=lambda item: str(item["id"]),
    )
    if len(strategies) != 25:
        store.db.close()
        raise RuntimeError(
            f"expected 25 public builtin strategies, found {len(strategies)}"
        )

    policy = BacktestResultPolicy(
        include_monte_carlo=False,
        include_curves=False,
        include_trades=False,
        include_per_symbol_stats=False,
        include_return_distribution=False,
        include_benchmark=False,
        include_strategy_info=True,
    )
    payload = {
        "run_spec": _build_run_spec(data_dir, len(strategies)),
        "strategies": [],
    }
    _write_json(output_dir / "results.json", payload)

    total_started = time.perf_counter()
    for index, strategy in enumerate(strategies, start=1):
        started = time.perf_counter()
        raw_result = service.run(
            _make_config(str(strategy["id"])),
            result_policy=policy,
        )
        result = _slim_result(strategy, raw_result)
        payload["strategies"].append(result)
        _write_json(output_dir / "results.json", payload)
        total_return = result["stats"].get("total_return")
        return_text = (
            f"{float(total_return):+.2%}" if total_return is not None else "n/a"
        )
        print(
            f"[{index:02d}/{len(strategies)}] {result['strategy_name']}: "
            f"return={return_text}, trades={result['stats'].get('n_trades', 0)}, "
            f"error={result['error'] or '-'}, elapsed={time.perf_counter() - started:.1f}s",
            flush=True,
        )

    payload["run_spec"]["completed_at"] = datetime.now(UTC).isoformat()
    payload["run_spec"]["elapsed_seconds"] = round(
        time.perf_counter() - total_started, 1
    )
    _write_json(output_dir / "results.json", payload)
    return payload, service, store


def _daily_audit(store: DataStore) -> dict[str, Any]:
    row = store.db.execute(
        """
        SELECT
            count(*) AS rows,
            count(DISTINCT symbol) AS symbols,
            count(DISTINCT date) AS trading_days,
            min(date) AS earliest_date,
            max(date) AS latest_date,
            count(*) FILTER (
                WHERE open IS NULL OR high IS NULL OR low IS NULL
                   OR close IS NULL OR volume IS NULL
            ) AS null_ohlcv_rows,
            count(*) FILTER (
                WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
            ) AS nonpositive_price_rows,
            count(*) FILTER (
                WHERE high < greatest(open, close, low)
                   OR low > least(open, close, high)
            ) AS ohlc_relation_errors,
            count(*) FILTER (WHERE volume < 0 OR amount < 0)
                AS negative_volume_amount_rows
        FROM kline_daily
        WHERE date BETWEEN ? AND ?
        """,
        [REQUESTED_START, EFFECTIVE_END],
    ).fetchone()
    names = [
        "rows",
        "symbols",
        "trading_days",
        "earliest_date",
        "latest_date",
        "null_ohlcv_rows",
        "nonpositive_price_rows",
        "ohlc_relation_errors",
        "negative_volume_amount_rows",
    ]
    result = dict(zip(names, row, strict=True))
    duplicate = store.db.execute(
        """
        SELECT count(*) AS duplicate_keys, coalesce(sum(n - 1), 0) AS duplicate_rows
        FROM (
            SELECT symbol, date, count(*) AS n
            FROM kline_daily
            WHERE date BETWEEN ? AND ?
            GROUP BY symbol, date
            HAVING count(*) > 1
        )
        """,
        [REQUESTED_START, EFFECTIVE_END],
    ).fetchone()
    result["duplicate_keys"] = int(duplicate[0])
    result["duplicate_rows"] = int(duplicate[1])
    for key in ("earliest_date", "latest_date"):
        result[key] = str(result[key])
    return {key: int(value) if isinstance(value, np.integer) else value for key, value in result.items()}


def _enriched_audit(store: DataStore) -> dict[str, Any]:
    row = store.db.execute(
        """
        SELECT
            count(*) AS rows,
            count(DISTINCT symbol) AS symbols,
            count(DISTINCT date) AS trading_days,
            min(date) AS earliest_date,
            max(date) AS latest_date
        FROM kline_enriched
        WHERE date BETWEEN ? AND ?
        """,
        [REQUESTED_START, EFFECTIVE_END],
    ).fetchone()
    return {
        "rows": int(row[0]),
        "symbols": int(row[1]),
        "trading_days": int(row[2]),
        "earliest_date": str(row[3]),
        "latest_date": str(row[4]),
    }


def _benchmark_audit(repo: KlineRepository) -> dict[str, Any]:
    frame = repo.get_index_daily(
        "000001.SH",
        REQUESTED_START,
        EFFECTIVE_END,
        ["symbol", "date", "close"],
    ).sort("date")
    closes = frame.get_column("close").cast(pl.Float64).to_numpy()
    returns = closes[1:] / closes[:-1] - 1.0
    equity = closes / closes[0]
    drawdown = equity / np.maximum.accumulate(equity) - 1.0
    years = max((frame.get_column("date")[-1] - frame.get_column("date")[0]).days / 365.25, 1e-9)
    annual_return = float((closes[-1] / closes[0]) ** (1.0 / years) - 1.0)
    sharpe = (
        float(np.mean(returns) / np.std(returns, ddof=1) * math.sqrt(252))
        if returns.size > 1 and np.std(returns, ddof=1) > 0
        else 0.0
    )
    return {
        "symbol": "000001.SH",
        "name": "上证指数",
        "rows": int(frame.height),
        "earliest_date": str(frame.get_column("date")[0]),
        "latest_date": str(frame.get_column("date")[-1]),
        "first_close": round(float(closes[0]), 2),
        "last_close": round(float(closes[-1]), 2),
        "total_return": round(float(closes[-1] / closes[0] - 1.0), 4),
        "annual_return": round(annual_return, 4),
        "max_drawdown": round(float(np.min(drawdown)), 4),
        "sharpe": round(sharpe, 2),
    }


def _build_audit(
    data_dir: Path,
    service: StrategyBacktestService,
    store: DataStore,
    payload: dict[str, Any],
) -> dict[str, Any]:
    daily = _daily_audit(store)
    enriched = _enriched_audit(store)
    governance = _load_governance(data_dir)
    errors = [item for item in payload["strategies"] if item["error"]]
    shapes = {
        tuple(item["stats"].get("market_matrix_shape", []))
        for item in payload["strategies"]
        if item["stats"].get("market_matrix_shape")
    }
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "requested_range": {
            "start": str(REQUESTED_START),
            "end": str(EFFECTIVE_END),
        },
        "effective_range": {
            "start": daily["earliest_date"],
            "end": daily["latest_date"],
        },
        "daily": daily,
        "enriched": enriched,
        "daily_enriched_row_match": daily["rows"] == enriched["rows"],
        "daily_enriched_date_match": (
            daily["earliest_date"] == enriched["earliest_date"]
            and daily["latest_date"] == enriched["latest_date"]
        ),
        "instrument_status_governance": governance,
        "benchmark": _benchmark_audit(service.engine.repo),
        "backtest": {
            "strategy_count": len(payload["strategies"]),
            "error_count": len(errors),
            "errors": [
                {
                    "strategy_id": item["strategy_id"],
                    "error": item["error"],
                }
                for item in errors
            ],
            "market_matrix_shapes": [list(shape) for shape in sorted(shapes)],
        },
    }


def _summary_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    ordered = sorted(
        payload["strategies"],
        key=lambda item: (
            item["error"] is not None,
            -float(item["stats"].get("total_return", -math.inf)),
        ),
    )
    rows: list[dict[str, Any]] = []
    for rank, item in enumerate(ordered, start=1):
        stats = item["stats"]
        avg_loss = float(stats.get("avg_loss") or 0.0)
        avg_win_loss_ratio = (
            round(float(stats.get("avg_win") or 0.0) / avg_loss, 2)
            if avg_loss
            else 0.0
        )
        rows.append({
            "rank": rank,
            "strategy_id": item["strategy_id"],
            "strategy_name": item["strategy_name"],
            "total_return": stats.get("total_return"),
            "annual_return": stats.get("annual_return"),
            "max_drawdown": stats.get("max_drawdown"),
            "sharpe": stats.get("sharpe"),
            "sortino": stats.get("sortino"),
            "calmar": stats.get("calmar"),
            "win_rate": stats.get("win_rate"),
            "avg_win_loss_ratio": avg_win_loss_ratio,
            "n_trades": stats.get("n_trades"),
            "avg_pnl": stats.get("avg_pnl"),
            "median_pnl": stats.get("median_pnl"),
            "avg_win": stats.get("avg_win"),
            "avg_loss": stats.get("avg_loss"),
            "best_trade": stats.get("best"),
            "worst_trade": stats.get("worst"),
            "avg_holding_days": stats.get("avg_holding_days"),
            "avg_exposure": stats.get("avg_exposure"),
            "max_exposure": stats.get("max_exposure"),
            "final_equity": stats.get("final_equity"),
            "strategy_matches": (stats.get("selection") or {}).get("strategy_matches"),
            "elapsed_ms": item["elapsed_ms"],
            "error": item["error"],
        })
    return rows


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _comparison_rows(
    rows: list[dict[str, Any]],
    previous_results: Path,
) -> list[dict[str, Any]]:
    if not previous_results.exists():
        return []
    previous_payload = json.loads(previous_results.read_text(encoding="utf-8"))
    previous = {
        item["strategy_id"]: item
        for item in previous_payload.get("strategies", [])
    }
    comparison = []
    for row in rows:
        old = previous.get(row["strategy_id"])
        if old is None:
            continue
        old_stats = old["stats"]
        comparison.append({
            "strategy_id": row["strategy_id"],
            "strategy_name": row["strategy_name"],
            "old_total_return": old_stats.get("total_return"),
            "governed_total_return": row["total_return"],
            "total_return_delta": round(
                float(row["total_return"]) - float(old_stats.get("total_return", 0.0)),
                4,
            ),
            "old_max_drawdown": old_stats.get("max_drawdown"),
            "governed_max_drawdown": row["max_drawdown"],
            "max_drawdown_delta": round(
                float(row["max_drawdown"]) - float(old_stats.get("max_drawdown", 0.0)),
                4,
            ),
            "old_win_rate": old_stats.get("win_rate"),
            "governed_win_rate": row["win_rate"],
            "win_rate_delta": round(
                float(row["win_rate"]) - float(old_stats.get("win_rate", 0.0)),
                4,
            ),
            "old_n_trades": old_stats.get("n_trades"),
            "governed_n_trades": row["n_trades"],
            "n_trades_delta": int(row["n_trades"]) - int(old_stats.get("n_trades", 0)),
        })
    return comparison


def _pct(value: Any) -> str:
    return f"{float(value):.2%}"


def _num(value: Any, digits: int = 2) -> str:
    return f"{float(value):,.{digits}f}"


def _render_report(
    payload: dict[str, Any],
    audit: dict[str, Any],
    rows: list[dict[str, Any]],
    comparisons: list[dict[str, Any]],
) -> str:
    successful = [row for row in rows if not row["error"]]
    positive = [row for row in successful if float(row["total_return"]) > 0]
    best = successful[0]
    benchmark = audit["benchmark"]
    governance = audit["instrument_status_governance"]
    comparison_by_id = {row["strategy_id"]: row for row in comparisons}
    best_comparison = comparison_by_id.get(best["strategy_id"])

    lines = [
        "# 治理后全市场 25 个内置策略历史回测",
        "",
        "## 核心结论",
        "",
        f"- 25 个公开内置策略全部完成，运行错误 `{audit['backtest']['error_count']}` 个；"
        f"正收益策略 `{len(positive)}` 个。",
        f"- 总收益排名第一为 **{best['strategy_name']}**：总收益 `{_pct(best['total_return'])}`、"
        f"年化 `{_pct(best['annual_return'])}`、最大回撤 `{_pct(best['max_drawdown'])}`、"
        f"夏普 `{_num(best['sharpe'])}`、交易 `{int(best['n_trades']):,}` 笔。",
        f"- 同期上证指数价格收益 `{_pct(benchmark['total_return'])}`、"
        f"最大回撤 `{_pct(benchmark['max_drawdown'])}`。",
    ]
    if best_comparison:
        lines.append(
            f"- 相比治理前旧报告，第一名同策略总收益变化 "
            f"`{float(best_comparison['total_return_delta']):+.2%}`，交易数变化 "
            f"`{int(best_comparison['n_trades_delta']):+,}` 笔。该差异还包含当前工作区"
            "已保留的通用回测撮合修复，不能解释为纯 ST 治理效应。"
        )

    lines.extend([
        "",
        "## 回测口径",
        "",
        f"- 区间：`{REQUESTED_START}` 至 `{EFFECTIVE_END}`；实际首个交易日 "
        f"`{audit['effective_range']['start']}`。",
        f"- 数据：股票日线与 enriched 各 `{audit['daily']['rows']:,}` 行，"
        f"`{audit['daily']['symbols']:,}` 只股票，"
        f"`{audit['daily']['trading_days']:,}` 个交易日。",
        "- 成交：信号后下一交易日开盘成交；初始资金 100 万；最多 10 个持仓；"
        "100% 最大敞口；等权。",
        "- 成本：佣金万 2 双边、卖出印花税千 1、滑点 5bp 双边。",
        "- 参数：各策略当前默认参数；未启用市场环境过滤和分钟成交。",
        f"- 代码：Git `{payload['run_spec']['git_head']}`；未提交工作区差异指纹 "
        f"`{payload['run_spec']['worktree_diff_sha256']}`。",
        "",
        "## ST 治理口径",
        "",
        f"- F10 成功解析 `{governance['fetched_successfully']:,}` 只；识别历史风险标的 "
        f"`{governance['risk_symbols_current']:,}` 只、风险区间行 "
        f"`{governance['risk_rows_current']:,}` 条。",
        f"- 历史事件 `{governance['event_count']:,}` 条；治理生成区间 "
        f"`{governance['governed_interval_count']:,}` 条；当前发布状态表 "
        f"`{governance['published_rows_current']:,}` 行。",
        f"- 拉取/解析未知的 `{governance['quarantined_symbol_count']}` 只股票按全历史"
        "风险警示隔离，采用 fail-closed 口径。",
        f"- 状态文件 SHA-256：`{governance['history_sha256']}`。",
        "",
        "## 总收益排名",
        "",
        "| 排名 | 策略 | 总收益 | 年化 | 最大回撤 | 夏普 | 胜率 | 平均盈亏比 | "
        "交易数 | 平均持有 | 期末资金 |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for row in rows:
        if row["error"]:
            lines.append(
                f"| {row['rank']} | {row['strategy_name']} | error | - | - | - | - | - | "
                f"- | - | - |"
            )
            continue
        lines.append(
            f"| {row['rank']} | {row['strategy_name']} | {_pct(row['total_return'])} | "
            f"{_pct(row['annual_return'])} | {_pct(row['max_drawdown'])} | "
            f"{_num(row['sharpe'])} | {_pct(row['win_rate'])} | "
            f"{_num(row['avg_win_loss_ratio'])} | {int(row['n_trades']):,} | "
            f"{_num(row['avg_holding_days'], 1)} 日 | {_num(row['final_equity'])} |"
        )

    lines.extend([
        "",
        "## 数据与运行校验",
        "",
        f"- 日线空 OHLCV：`{audit['daily']['null_ohlcv_rows']}`；非正价格："
        f"`{audit['daily']['nonpositive_price_rows']}`；OHLC 关系错误："
        f"`{audit['daily']['ohlc_relation_errors']}`；负成交量/金额："
        f"`{audit['daily']['negative_volume_amount_rows']}`。",
        f"- `(symbol, date)` 重复键：`{audit['daily']['duplicate_keys']}`；日线与 "
        f"enriched 行数一致：`{str(audit['daily_enriched_row_match']).lower()}`。",
        f"- 策略运行错误：`{audit['backtest']['error_count']}`；矩阵形状："
        f"`{audit['backtest']['market_matrix_shapes']}`。",
        "",
        "## 重要限制",
        "",
        "- 股票日线仍来自当前本地股票目录，缺少历史已退市股票，幸存者偏差尚未消除；"
        "本报告修正的是历史 ST/*ST 时点错配，不等于完整 point-in-time 股票池。",
        f"- `{governance['quarantined_symbol_count']}` 只无法可靠解析的股票被全历史隔离，"
        "这是保守处理，会减少候选样本。",
        "- 与治理前报告的差异同时受到历史状态治理和当前工作区通用撮合修复影响；"
        "如需量化单一治理效应，应固定同一代码版本另做 A/B。",
        "- 上证指数为价格指数，不含分红；策略结果包含交易成本，两者不是完全同口径"
        "的可投资组合比较。",
        "",
        "## 文件",
        "",
        "- `summary.csv`：完整策略指标。",
        "- `comparison-vs-pre-governance.csv`：与治理前旧报告逐策略对照。",
        "- `results.json`：逐策略统计、配置与 provenance。",
        "- `data-audit.json`：数据质量、治理状态和基准审计。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_DIR / "data",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_DIR
        / "data"
        / "reports"
        / "builtin-backtest-20160101-20260930-st-governed",
    )
    parser.add_argument(
        "--previous-results",
        type=Path,
        default=REPO_DIR
        / "data"
        / "reports"
        / "builtin-backtest-20160101-20261008"
        / "results.json",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="reuse a completed results.json checkpoint and only rebuild audit/report files",
    )
    args = parser.parse_args()
    data_dir = args.data_dir.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.report_only:
        results_path = output_dir / "results.json"
        payload = json.loads(results_path.read_text(encoding="utf-8"))
        if (
            len(payload.get("strategies", [])) != 25
            or any(item.get("error") for item in payload["strategies"])
        ):
            raise RuntimeError("report-only mode requires a successful 25-strategy checkpoint")
        service, _, store = _open_service(data_dir)
    else:
        payload, service, store = _run_strategies(data_dir, output_dir)
    try:
        audit = _build_audit(data_dir, service, store, payload)
    finally:
        store.db.close()

    rows = _summary_rows(payload)
    comparisons = _comparison_rows(rows, args.previous_results.resolve())
    _write_csv(output_dir / "summary.csv", SUMMARY_FIELDS, rows)
    if comparisons:
        _write_csv(
            output_dir / "comparison-vs-pre-governance.csv",
            list(comparisons[0]),
            comparisons,
        )
    _write_json(output_dir / "data-audit.json", audit)
    (output_dir / "report.md").write_text(
        _render_report(payload, audit, rows, comparisons),
        encoding="utf-8",
    )
    print(f"report: {output_dir / 'report.md'}", flush=True)
    return 0 if audit["backtest"]["error_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
