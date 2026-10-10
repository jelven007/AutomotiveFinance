"""Measure full-pool rustdx quote latency with isolated persistent pools.

Run from backend:
    uv run --no-sync python scripts/benchmark_rustdx_quotes.py --connections 8-30
    uv run --no-sync python scripts/benchmark_rustdx_quotes.py --dry-run
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import multiprocessing
import os
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BACKEND = Path(__file__).resolve().parents[1]
BEIJING = ZoneInfo("Asia/Shanghai")
MIN_P95_SAMPLES = 100


def beijing_now() -> str:
    return datetime.now(BEIJING).isoformat(timespec="milliseconds")


def connection_counts(text: str) -> list[int]:
    counts = []
    try:
        for part in text.split(","):
            bounds = [int(value.strip()) for value in part.split("-")]
            if len(bounds) == 1:
                values = bounds
            elif len(bounds) == 2 and bounds[0] <= bounds[1]:
                values = range(bounds[0], bounds[1] + 1)
            else:
                raise ValueError
            for value in values:
                if not 8 <= value <= 30:
                    raise ValueError
                if value not in counts:
                    counts.append(value)
    except ValueError as exc:
        raise ValueError("连接数必须在 8-30 内, 例如 8-30 或 8,12,16,20,24,30") from exc
    return counts


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank quantile; small samples are never interpolated downward."""
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def load_universe(path: Path) -> tuple[list[str], dict]:
    import polars as pl

    from app.data_providers.instrument_status import (
        current_a_share_identity,
        is_delisted_name,
    )

    frame = pl.read_parquet(path)
    if not {"symbol", "code"} & set(frame.columns):
        raise ValueError("股票池缺少 symbol/code 列")
    columns = [
        column for column in ("symbol", "code", "exchange", "name", "type", "as_of")
        if column in frame.columns
    ]
    symbols = set()
    dates = set()
    for row in frame.select(columns).iter_rows(named=True):
        if row.get("type") not in (None, "", "stock") or is_delisted_name(row.get("name")):
            continue
        identity = current_a_share_identity(
            row.get("symbol"), code=row.get("code"), exchange=row.get("exchange")
        )
        if identity is not None:
            symbols.add(identity[0])
            if row.get("as_of") is not None:
                dates.add(str(row["as_of"]))
    ordered = sorted(symbols)
    if not ordered:
        raise ValueError("过滤后有效沪深股票池为空")
    market_counts = {
        exchange: sum(symbol.endswith(f".{exchange}") for symbol in ordered)
        for exchange in ("SH", "SZ")
    }
    return ordered, {
        "input_file": str(path.resolve()),
        "input_rows": frame.height,
        "symbol_count": len(ordered),
        "symbols_sha256": hashlib.sha256("\n".join(ordered).encode()).hexdigest(),
        "as_of_dates": sorted(dates),
        "market_counts": market_counts,
        "quote_batches": sum(math.ceil(count / 60) for count in market_counts.values()),
    }


def assess_snapshot(rows: list[dict], expected: set[str]) -> dict:
    returned = set()
    valid = set()
    duplicates = unexpected = invalid = 0
    source_times = set()
    for row in rows:
        symbol = row.get("symbol")
        if symbol not in expected:
            unexpected += 1
            continue
        if symbol in returned:
            duplicates += 1
        returned.add(symbol)
        price = row.get("last_price")
        # Zero is valid for suspended/not-yet-traded stocks. Null/NaN is not.
        if isinstance(price, (int, float)) and math.isfinite(price) and price >= 0:
            valid.add(symbol)
        else:
            invalid += 1
        if row.get("source_time"):
            source_times.add(str(row["source_time"]))
    complete = (
        returned == valid == expected and duplicates == unexpected == invalid == 0
    )
    return {
        "status": "ok" if complete else "incomplete",
        "returned_rows": len(rows),
        "unique_symbols": len(returned),
        "coverage": len(returned) / len(expected),
        "valid_coverage": len(valid) / len(expected),
        "missing_symbols": sorted(expected - returned),
        "invalid_price_rows": invalid,
        "duplicate_rows": duplicates,
        "unexpected_rows": unexpected,
        "source_time_values": sorted(source_times),
    }


@dataclass(frozen=True)
class ProfileConfig:
    connections: int
    rounds: int = 100
    warmup: int = 5
    interval: float = 1.0
    server: str = "117.34.114.13:7709"
    socket_timeout: int = 8
    round_timeout: float = 60.0


def measure_profile(
    config: ProfileConfig,
    symbols: list[str],
    provider: Any,
    client: Any,
    emit: Callable[[dict], None],
) -> None:
    expected = set(symbols)
    phases = [("cold", 1)]
    phases.extend(("warmup", i + 1) for i in range(config.warmup))
    phases.extend(("measure", i + 1) for i in range(config.rounds))
    last_start = None
    last_stats = {}
    try:
        for phase, number in phases:
            if last_start is not None:
                delay = config.interval - (time.perf_counter() - last_start)
                if delay > 0:
                    time.sleep(delay)
            emit({"kind": "started", "phase": phase, "round": number})
            fetched_at = beijing_now()
            last_start = time.perf_counter()
            try:
                rows = provider.get_auction_snapshot(symbols)
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - last_start) * 1000
                sample = {
                    "status": "error", "coverage": 0.0, "valid_coverage": 0.0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            else:
                elapsed_ms = (time.perf_counter() - last_start) * 1000
                sample = assess_snapshot(rows, expected)
            last_stats = client.stats()
            sample.update({
                "phase": phase,
                "round": number,
                "fetched_at": fetched_at,
                "finished_at": beijing_now(),
                "elapsed_ms": elapsed_ms,
                "pool_stats": last_stats,
            })
            emit({"kind": "sample", "sample": sample})
    finally:
        provider.close()
    emit({"kind": "done", "pool_stats": last_stats})


def profile_worker(config: ProfileConfig, symbols: list[str], sender: Any) -> None:
    # spawn provides a fresh singleton; only this child's environment changes.
    os.environ["RUSTDX_QUOTE_CONNECTIONS"] = str(config.connections)
    os.environ["RUSTDX_SERVER"] = config.server
    os.environ["RUSTDX_TIMEOUT"] = str(config.socket_timeout)
    sys.path.insert(0, str(BACKEND))
    with sender:
        try:
            from app.plugins.rustdx.client import RustdxClient, availability
            from app.plugins.rustdx.provider import RustdxProvider

            available, detail = availability()
            if not available:
                raise RuntimeError(detail)
            provider = RustdxProvider()
            client = RustdxClient()
            try:
                sender.send({
                    "kind": "ready", "pid": os.getpid(),
                    "pool_stats": client.stats(), "bridge": detail,
                })
                measure_profile(config, symbols, provider, client, sender.send)
            finally:
                # Also handles failure before the measurement loop starts.
                provider.close()
        except Exception as exc:
            sender.send({"kind": "fatal", "error": f"{type(exc).__name__}: {exc}"})


def stop_process(process: Any) -> None:
    process.join(timeout=1)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
    if process.is_alive():
        process.kill()
        process.join(timeout=2)


def supervise_profile(
    config: ProfileConfig,
    symbols: list[str],
    *,
    worker_target: Callable = profile_worker,
    on_sample: Callable[[dict], None] | None = None,
) -> dict:
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=worker_target, args=(config, symbols, sender))
    result = {
        "connections": config.connections, "status": "error", "samples": [],
        "started_at": beijing_now(),
    }
    running = None
    deadline = time.monotonic() + 30
    process.start()
    sender.close()
    result["pid"] = process.pid
    try:
        while True:
            if receiver.poll(0.1):
                try:
                    event = receiver.recv()
                except EOFError:
                    result["error"] = "子进程在完成前退出"
                    break
                kind = event["kind"]
                if kind == "ready":
                    result["initial_pool_stats"] = event["pool_stats"]
                    result["bridge"] = event.get("bridge")
                    deadline = time.monotonic() + config.round_timeout + config.interval + 5
                elif kind == "started":
                    running = {**event, "started": time.monotonic()}
                    deadline = running["started"] + config.round_timeout
                elif kind == "sample":
                    result["samples"].append(event["sample"])
                    if on_sample is not None:
                        on_sample(event["sample"])
                    running = None
                    deadline = time.monotonic() + config.interval + config.round_timeout + 5
                elif kind == "done":
                    result["status"] = "completed"
                    result["final_pool_stats"] = event["pool_stats"]
                    break
                elif kind == "fatal":
                    result["error"] = event["error"]
                    break
            elif time.monotonic() >= deadline:
                result["status"] = "timed_out"
                result["error"] = f"子进程超时(单轮上限 {config.round_timeout:g}s)"
                if running is not None:
                    result["samples"].append({
                        "phase": running["phase"], "round": running["round"],
                        "status": "timed_out", "coverage": 0.0, "valid_coverage": 0.0,
                        "elapsed_ms": (time.monotonic() - running["started"]) * 1000,
                        "latency_censored": True,
                        "error": result["error"],
                    })
                break
            elif not process.is_alive():
                result["error"] = f"子进程异常退出, exitcode={process.exitcode}"
                break
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        result["error"] = "用户中断"
    finally:
        if running is not None:
            already_recorded = any(
                sample["phase"] == running["phase"] and sample["round"] == running["round"]
                for sample in result["samples"]
            )
            if not already_recorded:
                result["samples"].append({
                    "phase": running["phase"], "round": running["round"],
                    "status": result["status"], "coverage": 0.0, "valid_coverage": 0.0,
                    "elapsed_ms": (time.monotonic() - running["started"]) * 1000,
                    "latency_censored": True,
                    "error": result.get("error", "子进程提前退出"),
                })
        stop_process(process)
        receiver.close()
        result["exitcode"] = process.exitcode
        result["finished_at"] = beijing_now()
        process.close()
    return result


def summarize_profile(profile: dict, requested_rounds: int) -> dict:
    measured = [sample for sample in profile["samples"] if sample["phase"] == "measure"]
    successful = [sample for sample in measured if sample["status"] == "ok"]
    latencies = [sample["elapsed_ms"] for sample in successful]
    all_latencies = [sample["elapsed_ms"] for sample in measured]
    cold = next((sample for sample in profile["samples"] if sample["phase"] == "cold"), {})
    observed = [
        sample["pool_stats"]["total_connections"]
        for sample in measured if "total_connections" in sample.get("pool_stats", {})
    ]
    config_matches = bool(measured) and all(
        sample.get("pool_stats", {}).get("max_connections") == profile["connections"]
        for sample in measured
    )
    mean = sum(latencies) / len(latencies) if latencies else None
    return {
        "connections": profile["connections"],
        "status": profile["status"],
        "requested_rounds": requested_rounds,
        "attempted_rounds": len(measured),
        "successful_rounds": len(successful),
        "failed_rounds": len(measured) - len(successful),
        "unattempted_rounds": max(0, requested_rounds - len(measured)),
        "success_rate": len(successful) / requested_rounds,
        "min_coverage": min((sample["coverage"] for sample in measured), default=None),
        "min_valid_coverage": min(
            (sample["valid_coverage"] for sample in measured), default=None
        ),
        "cold_ms": cold.get("elapsed_ms"),
        "cold_status": cold.get("status"),
        "p50_ms": percentile(latencies, 0.50),
        "p95_ms": percentile(latencies, 0.95),
        "p99_ms": percentile(latencies, 0.99),
        "mean_ms": mean,
        "max_ms": max(latencies, default=None),
        "all_attempts_p95_ms": percentile(all_latencies, 0.95),
        "all_attempts_censored": any(
            sample["status"] == "timed_out" or sample.get("latency_censored", False)
            for sample in measured
        ),
        "snapshot_capacity_per_second": 1000 / mean if mean else None,
        "sample_sufficient": len(successful) >= MIN_P95_SAMPLES,
        "connection_config_matches": config_matches,
        "observed_connections_min": min(observed, default=None),
        "observed_connections_max": max(observed, default=None),
    }


def profile_passed(summary: dict, maximum_ms: float) -> bool:
    return (
        summary["status"] == "completed"
        and summary["sample_sufficient"]
        and summary["connection_config_matches"]
        and summary["success_rate"] == 1
        and summary["p95_ms"] is not None
        and summary["p95_ms"] <= maximum_ms
    )


def write_results(output: Path, report: dict) -> None:
    # Checkpoint after every profile, so Ctrl+C does not lose finished profiles.
    temporary = output / "results.json.tmp"
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    temporary.replace(output / "results.json")
    summaries = [profile["summary"] for profile in report["profiles"]]
    with (output / "summary.csv").open("w", newline="", encoding="utf-8-sig") as handle:
        if summaries:
            writer = csv.DictWriter(handle, fieldnames=list(summaries[0]))
            writer.writeheader()
            writer.writerows(summaries)
    with (output / "samples.jsonl").open("w", encoding="utf-8") as handle:
        for profile in report["profiles"]:
            for sample in profile["samples"]:
                handle.write(json.dumps(
                    {"connections": profile["connections"], **sample},
                    ensure_ascii=False, allow_nan=False,
                ) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="rustdx 全股票池持久连接 P95 压测")
    parser.add_argument("--connections", default="8-30", help="逐档范围或逗号列表(默认 8-30)")
    parser.add_argument("--rounds", type=int, default=100, help="每档正式采样轮数(默认 100)")
    parser.add_argument("--warmup", type=int, default=5, help="冷启动后预热轮数(默认 5)")
    parser.add_argument("--interval", type=float, default=1, help="轮次开始间隔秒; 0 为连续压测")
    parser.add_argument("--socket-timeout", type=int, default=8, help="TCP 超时秒, 2-60")
    parser.add_argument("--round-timeout", type=float, default=60, help="整轮超时秒, 超时终止该档")
    parser.add_argument(
        "--server", default=os.getenv("RUSTDX_SERVER", "").strip() or "117.34.114.13:7709",
        help="固定通达信 IP:端口; 所有档位使用同一服务器",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=BACKEND.parent / "data", help="本地股票池所在数据目录"
    )
    parser.add_argument("--output", type=Path, help="新的结果目录, 默认 data/research/rustdx-quotes-*")
    parser.add_argument("--max-p95-ms", type=float, help="可选验收上限; 失败或样本不足返回 1")
    parser.add_argument("--dry-run", action="store_true", help="仅检查股票池和参数, 不连接行情服务器")
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(BACKEND))
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        counts = connection_counts(args.connections)
        if args.rounds < 1 or args.warmup < 0:
            raise ValueError("rounds 必须 >= 1, warmup 必须 >= 0")
        if not math.isfinite(args.interval) or not 0 <= args.interval <= 60:
            raise ValueError("interval 必须在 0-60 秒内")
        if not 2 <= args.socket_timeout <= 60:
            raise ValueError("socket-timeout 必须在 2-60 秒内")
        if not math.isfinite(args.round_timeout) or args.round_timeout <= 0:
            raise ValueError("round-timeout 必须为有限正数")
        if args.max_p95_ms is not None and (
            not math.isfinite(args.max_p95_ms) or args.max_p95_ms <= 0
        ):
            raise ValueError("max-p95-ms 必须为有限正数")
        symbols, universe = load_universe(args.data_dir / "instruments" / "instruments.parquet")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    report = {
        "schema_version": 1,
        "status": "running",
        "started_at": beijing_now(),
        "environment": {"platform": platform.platform(), "python": sys.version},
        "universe": universe,
        "parameters": {
            "connections": counts, "rounds": args.rounds, "warmup": args.warmup,
            "interval_seconds": args.interval, "server": args.server,
            "socket_timeout_seconds": args.socket_timeout,
            "round_timeout_seconds": args.round_timeout, "max_p95_ms": args.max_p95_ms,
        },
        "method": {
            "latency_scope": "Provider 显式股票快照调用, 包含原生采集、JSON 解码和字段标准化",
            "quantile": "nearest_rank",
            "success": "全部股票返回, 价格非空且有限, 允许零价, 无重复/多余记录",
            "p95_min_successful_samples": MIN_P95_SAMPLES,
            "cadence": "开始到开始的最小间隔; 慢轮次完成后才启动下一轮",
            "source_time": "单票事件时间, 仅留证, 不作为行情新鲜度判定",
        },
        "profiles": [],
    }
    print(
        f"股票池 {len(symbols)} 只 / {universe['quote_batches']} 批, "
        f"连接档位 {counts}, 每档 {args.rounds} 个正式样本, 服务器 {args.server}", flush=True,
    )
    if args.dry_run:
        report["status"] = "dry_run"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.rounds < MIN_P95_SAMPLES:
        print(f"样本少于 {MIN_P95_SAMPLES}: 仅作连通性/冒烟检查, 不能通过 P95 验收。", flush=True)
    output = args.output or args.data_dir / "research" / (
        "rustdx-quotes-" + datetime.now(BEIJING).strftime("%Y%m%dT%H%M%S%f")
    )
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        parser.error(f"结果目录必须为可创建的新目录: {exc}")
    print(f"结果目录: {output.resolve()}", flush=True)
    print("连接  完整轮次    P50(ms)    P95(ms)    P99(ms)    冷启动(ms)  状态", flush=True)
    interrupted = False
    try:
        for count in counts:
            config = ProfileConfig(
                connections=count, rounds=args.rounds, warmup=args.warmup,
                interval=args.interval, server=args.server, socket_timeout=args.socket_timeout,
                round_timeout=args.round_timeout,
            )

            def progress(sample: dict, current_count: int = count) -> None:
                if sample["phase"] == "measure" and (
                    sample["round"] % 10 == 0 or sample["status"] != "ok"
                ):
                    print(
                        f"  {current_count} 连接 {sample['round']}/{args.rounds}: "
                        f"{sample['elapsed_ms']:.1f}ms {sample['status']} "
                        f"覆盖率 {sample['coverage']:.2%}", flush=True,
                    )

            profile = supervise_profile(config, symbols, on_sample=progress)
            summary = summarize_profile(profile, args.rounds)
            profile["summary"] = summary
            if args.max_p95_ms is not None:
                summary["passed"] = profile_passed(summary, args.max_p95_ms)
            report["profiles"].append(profile)
            write_results(output, report)
            numbers = [
                f"{summary[key]:10.1f}" if summary[key] is not None else f"{'N/A':>10}"
                for key in ("p50_ms", "p95_ms", "p99_ms", "cold_ms")
            ]
            print(
                f"{count:4}  {summary['successful_rounds']:4}/{args.rounds:<4} "
                f"{' '.join(numbers)}  {profile['status']}", flush=True,
            )
            if profile.get("error"):
                print(f"  {profile['error']}", flush=True)
            if profile["status"] == "interrupted":
                interrupted = True
                break
    except KeyboardInterrupt:
        interrupted = True
    report["status"] = "interrupted" if interrupted else "completed"
    report["finished_at"] = beijing_now()
    write_results(output, report)
    if interrupted:
        return 130
    summaries = [profile["summary"] for profile in report["profiles"]]
    if any(
        summary["status"] != "completed" or summary["success_rate"] < 1
        or not summary["connection_config_matches"]
        for summary in summaries
    ):
        return 1
    if args.max_p95_ms is not None and not all(summary["passed"] for summary in summaries):
        return 1
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
