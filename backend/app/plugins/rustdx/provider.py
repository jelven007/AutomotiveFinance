"""rustdx provider using the same normalized TDX contracts as mootdx."""

from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import polars as pl

from app.market_time import cn_now
from app.plugins.mootdx.provider import MootdxProvider, _symbol
from app.plugins.rustdx.client import RustdxClient

_DATASETS = (
    "realtime",
    "daily",
    "adj_factor",
    "minute",
    "depth5",
    "financial",
    "full_minute",
)
_MAX_CONNECTIONS = 35


@dataclass
class _RustdxConfig:
    name: str = "rustdx"
    display_name: str = "rustdx"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


class RustdxProvider(MootdxProvider):
    """Default TDX provider backed by rustdx-complete's native protocol engine."""

    name = "rustdx"
    env_prefix = "RUSTDX"
    enrich_instruments_with_finance = False

    def __init__(self, client_factory=RustdxClient) -> None:
        super().__init__(client_factory=client_factory)
        self.config = _RustdxConfig()

    def close(self) -> None:
        super().close()
        close_shared = getattr(self._client_factory, "close_shared", None)
        if callable(close_shared):
            close_shared()

    @classmethod
    def _workers(cls, symbol_count: int) -> int:
        try:
            configured = int(os.getenv("RUSTDX_WORKERS", str(_MAX_CONNECTIONS)))
        except ValueError:
            configured = _MAX_CONNECTIONS
        return max(1, min(configured, _MAX_CONNECTIONS, symbol_count))

    def _persistent_quotes(self, symbols: list[str], *, operation: str) -> list[dict]:
        del operation
        # One native call partitions the universe into 60-symbol requests and
        # schedules them across the process-wide pool. The native pool clamps
        # its connection count to 35, including concurrent API consumers.
        return self._client_factory().quotes(symbols)

    def iter_daily(
        self,
        symbols,
        start_time,
        end_time,
        asset_type="stock",
        on_chunk_done=None,
    ) -> Iterator[pl.DataFrame]:
        valid = [symbol for symbol in symbols if _symbol(symbol) and not symbol.endswith(".BJ")]
        if not valid:
            return
        workers = self._workers(len(valid))

        def fetch(partition):
            return list(
                super(RustdxProvider, self).iter_daily(
                    partition,
                    start_time,
                    end_time,
                    asset_type=asset_type,
                )
            )

        # Bounded windows stream results without submitting the entire universe
        # or holding years of market history in memory.
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rustdx-daily") as pool:
            done = 0
            for offset in range(0, len(valid), workers * 20):
                window = valid[offset : offset + workers * 20]
                partitions = [window[index::workers] for index in range(workers)]
                for frames in pool.map(fetch, partitions):
                    yield from frames
                done += len(window)
                if on_chunk_done:
                    on_chunk_done(done, len(valid))

    def get_intraday_latest(self, symbols, count=3, asset_type="stock") -> pl.DataFrame:
        del asset_type
        valid = [symbol for symbol in symbols if _symbol(symbol) and not symbol.endswith(".BJ")]
        if not valid:
            return pl.DataFrame()
        end = cn_now().replace(tzinfo=None)
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)

        def fetch(symbol):
            with self._client_factory() as client:
                raw = client.bars(symbol, frequency=8, offset=max(1, min(int(count), 800)))
            return self._minute_frame(raw, symbol, start, end)

        # Native pool is shared with quotes/history/finance and enforces 35
        # sockets across all consumers, including simultaneous calls.
        with ThreadPoolExecutor(
            max_workers=self._workers(len(valid)), thread_name_prefix="rustdx-latest"
        ) as pool:
            frames = [frame for frame in pool.map(fetch, valid) if not frame.is_empty()]
        return (
            pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "datetime"])
            if frames
            else pl.DataFrame()
        )

    def get_adj_factors(
        self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None
    ) -> pl.DataFrame:
        valid = [symbol for symbol in symbols if _symbol(symbol) and not symbol.endswith(".BJ")]
        if not valid:
            return super().get_adj_factors([], start_time, end_time, asset_type)
        workers = self._workers(len(valid))
        partitions = [valid[index::workers] for index in range(workers)]

        def fetch(partition):
            return super(RustdxProvider, self).get_adj_factors(
                partition,
                start_time,
                end_time,
                asset_type=asset_type,
            )

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="rustdx-adjustment"
        ) as pool:
            frames = []
            for index, frame in enumerate(pool.map(fetch, partitions), start=1):
                frames.append(frame)
                if on_chunk_done:
                    on_chunk_done(index, len(partitions))
        return pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "trade_date"])

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        if dataset not in _DATASETS:
            raise ValueError(f"rustdx 不支持数据集: {dataset}")
        return super().test_dataset(dataset, symbols)
