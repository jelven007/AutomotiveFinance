"""Offline checks for quote benchmark statistics and process supervision."""

from __future__ import annotations

import csv
import json
import multiprocessing
import os
import threading

import polars as pl
import pytest

from scripts import benchmark_rustdx_quotes as bench


def test_connection_range_and_invalid_inputs():
    assert bench.connection_counts("8-30") == list(range(8, 31))
    assert bench.connection_counts("8,12-14,30,8") == [8, 12, 13, 14, 30]
    for value in ("", "7", "31", "30-8", "8,", "abc"):
        with pytest.raises(ValueError):
            bench.connection_counts(value)


def test_nearest_rank_percentile_small_and_full_samples():
    assert bench.percentile([], 0.95) is None
    assert bench.percentile([10], 0.95) == 10
    assert bench.percentile(list(range(1, 101)), 0.95) == 95
    assert bench.percentile(list(range(1, 11)), 0.95) == 10


def test_pool_uses_current_supported_stocks_and_keeps_st(tmp_path):
    path = tmp_path / "instruments.parquet"
    pl.DataFrame(
        {
            "symbol": [
                "600000.SH", "600000.SH", "688001.SH", "300001.SZ",
                "000001.SZ", "002001.SZ", "600001.SH", "510300.SH", "399001.SZ",
                "900901.SH", "200001.SZ", "000001.SH",
            ],
            "name": [
                "浦发银行", "浦发银行", "科创股", "创业股",
                "*ST测试", "停牌股票", "退市股票", "ETF", "指数",
                "B股", "B股", "上证指数",
            ],
            "type": ["stock"] * 7 + ["etf", "index", "stock", "stock", "index"],
        }
    ).write_parquet(path)
    symbols, metadata = bench.load_universe(path)
    assert symbols == ["000001.SZ", "002001.SZ", "300001.SZ", "600000.SH", "688001.SH"]
    assert metadata["input_rows"] == 12
    assert metadata["symbol_count"] == 5
    assert len(metadata["symbols_sha256"]) == 64
    assert metadata["quote_batches"] == 2


def test_empty_pool_is_rejected(tmp_path):
    path = tmp_path / "empty.parquet"
    pl.DataFrame({"symbol": ["510300.SH"], "type": ["etf"]}).write_parquet(path)
    with pytest.raises(ValueError, match="股票池为空"):
        bench.load_universe(path)


def test_duplicate_unknown_or_invalid_price_cannot_fake_full_coverage():
    expected = {"600000.SH", "000001.SZ"}
    rows = [
        {"symbol": "600000.SH", "last_price": 10},
        {"symbol": "600000.SH", "last_price": 10},
        {"symbol": "510300.SH", "last_price": 4},
        {"symbol": "000001.SZ", "last_price": None},
    ]
    sample = bench.assess_snapshot(rows, expected)
    assert sample["status"] == "incomplete"
    assert sample["coverage"] == 1
    assert sample["valid_coverage"] == 0.5
    assert sample["duplicate_rows"] == 1
    assert sample["unexpected_rows"] == 1
    assert sample["invalid_price_rows"] == 1
    assert bench.assess_snapshot(
        [{"symbol": symbol, "last_price": 0} for symbol in expected], expected
    )["status"] == "ok"


def make_sample(phase, elapsed_ms, status="ok", **extra):
    return {
        "phase": phase,
        "round": 1,
        "elapsed_ms": elapsed_ms,
        "status": status,
        "coverage": 1 if status == "ok" else 0,
        "valid_coverage": 1 if status == "ok" else 0,
        "pool_stats": {"max_connections": 8, "total_connections": 8},
        **extra,
    }


def test_failure_and_cold_rounds_do_not_improve_successful_p95():
    samples = [
        make_sample("cold", 5000),
        make_sample("warmup", 3000),
        make_sample("measure", 100),
        make_sample("measure", 200),
        make_sample("measure", 1, "incomplete"),
        make_sample("measure", 2, "error"),
        make_sample("measure", 1000, "timed_out"),
    ]
    summary = bench.summarize_profile(
        {"connections": 8, "status": "timed_out", "samples": samples}, 6
    )
    assert summary["p95_ms"] == 200
    assert summary["all_attempts_p95_ms"] == 1000
    assert summary["cold_ms"] == 5000
    assert summary["successful_rounds"] == 2
    assert summary["failed_rounds"] == 3
    assert summary["unattempted_rounds"] == 1
    assert summary["success_rate"] == pytest.approx(2 / 6)
    assert summary["sample_sufficient"] is False
    assert summary["all_attempts_censored"] is True
    assert bench.profile_passed(summary, 500) is False


def test_threshold_requires_all_rounds_and_enough_samples():
    summary = bench.summarize_profile(
        {
            "connections": 8,
            "status": "completed",
            "samples": [make_sample("measure", i) for i in range(1, 101)],
        },
        100,
    )
    assert bench.profile_passed(summary, 95) is True
    assert bench.profile_passed(summary, 94) is False
    assert summary["p99_ms"] == 99


class FakeClient:
    def stats(self):
        return {"max_connections": 8, "total_connections": 8, "quote_batch_size": 60}


class FakeProvider:
    closed = False
    calls = 0

    def get_auction_snapshot(self, symbols):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("transport failed")
        return [{"symbol": symbol, "last_price": 10} for symbol in symbols]

    def close(self):
        self.closed = True


def test_measurement_keeps_failures_and_always_closes():
    provider = FakeProvider()
    events = []
    bench.measure_profile(
        bench.ProfileConfig(connections=8, rounds=2, warmup=0, interval=0),
        ["600000.SH"],
        provider,
        FakeClient(),
        events.append,
    )
    samples = [event["sample"] for event in events if event["kind"] == "sample"]
    assert [sample["phase"] for sample in samples] == ["cold", "measure", "measure"]
    assert [sample["status"] for sample in samples] == ["ok", "error", "ok"]
    assert samples[1]["error"] == "RuntimeError: transport failed"
    assert events[-1]["kind"] == "done"
    assert provider.closed


def test_emit_failure_also_closes_pool():
    provider = FakeProvider()

    def broken_emit(_event):
        raise RuntimeError("parent went away")

    with pytest.raises(RuntimeError, match="parent went away"):
        bench.measure_profile(
            bench.ProfileConfig(connections=8),
            ["600000.SH"], provider, FakeClient(), broken_emit,
        )
    assert provider.closed


def fake_worker(config, _symbols, sender):
    os.environ["RUSTDX_QUOTE_CONNECTIONS"] = str(config.connections)
    with sender:
        sender.send({"kind": "ready", "pid": os.getpid(), "pool_stats": {}})
        sample = make_sample(
            "measure", 100,
            pool_stats={
                "max_connections": int(os.environ["RUSTDX_QUOTE_CONNECTIONS"]),
                "total_connections": config.connections,
            },
        )
        sender.send({"kind": "sample", "sample": sample})
        sender.send({"kind": "done", "pool_stats": sample["pool_stats"]})


def stalled_worker(_config, _symbols, sender):
    with sender:
        sender.send({"kind": "ready", "pid": os.getpid(), "pool_stats": {}})
        sender.send({"kind": "started", "phase": "measure", "round": 1})
        threading.Event().wait(20)


def crashing_worker(_config, _symbols, sender):
    with sender:
        sender.send({"kind": "ready", "pid": os.getpid(), "pool_stats": {}})
        sender.send({"kind": "started", "phase": "measure", "round": 1})
        os._exit(9)


def test_connection_profiles_use_separate_processes(monkeypatch):
    monkeypatch.setenv("RUSTDX_QUOTE_CONNECTIONS", "35")
    results = [
        bench.supervise_profile(
            bench.ProfileConfig(connections=count, rounds=1, warmup=0, interval=0),
            ["600000.SH"], worker_target=fake_worker,
        )
        for count in (8, 30)
    ]
    assert results[0]["pid"] != results[1]["pid"]
    assert [result["samples"][0]["pool_stats"]["max_connections"] for result in results] == [
        8, 30,
    ]
    assert all(result["status"] == "completed" for result in results)
    assert os.environ["RUSTDX_QUOTE_CONNECTIONS"] == "35"


def test_timeout_preserves_censored_attempt_and_terminates_child():
    result = bench.supervise_profile(
        bench.ProfileConfig(connections=8, round_timeout=0.3, interval=0),
        ["600000.SH"], worker_target=stalled_worker,
    )
    assert result["status"] == "timed_out"
    assert result["samples"][-1]["status"] == "timed_out"
    assert result["samples"][-1]["elapsed_ms"] >= 300
    assert result["pid"] not in {child.pid for child in multiprocessing.active_children()}


def test_child_crash_is_counted_as_failed_attempt():
    result = bench.supervise_profile(
        bench.ProfileConfig(connections=8, rounds=2, interval=0),
        ["600000.SH"], worker_target=crashing_worker,
    )
    assert result["status"] == "error"
    assert result["exitcode"] == 9
    assert result["samples"][-1]["status"] == "error"
    assert result["samples"][-1]["latency_censored"] is True
    summary = bench.summarize_profile(result, 2)
    assert summary["failed_rounds"] == 1
    assert summary["unattempted_rounds"] == 1
    assert summary["all_attempts_censored"] is True


def test_output_files_preserve_raw_failures_and_summary(tmp_path):
    profile = {
        "connections": 8,
        "status": "completed",
        "samples": [make_sample("measure", 10, "incomplete")],
    }
    profile["summary"] = bench.summarize_profile(profile, 1)
    report = {"status": "completed", "profiles": [profile]}
    bench.write_results(tmp_path, report)
    assert json.loads((tmp_path / "results.json").read_text()) == report
    with (tmp_path / "summary.csv").open(newline="") as handle:
        summary = next(csv.DictReader(handle))
    assert summary["successful_rounds"] == "0"
    assert summary["p95_ms"] == ""
    sample = json.loads((tmp_path / "samples.jsonl").read_text())
    assert sample["status"] == "incomplete"
    assert sample["connections"] == 8


def test_dry_run_never_opens_native_pool_or_creates_outputs(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    (data_dir / "instruments").mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"]}).write_parquet(
        data_dir / "instruments" / "instruments.parquet"
    )
    output = tmp_path / "output"

    def unexpected_network(*_args, **_kwargs):
        pytest.fail("dry-run opened a worker")

    monkeypatch.setattr(bench, "supervise_profile", unexpected_network)
    assert bench.main(
        ["--dry-run", "--data-dir", str(data_dir), "--output", str(output)]
    ) == 0
    assert not output.exists()
