"""mootdx built-in provider with TickFlow-compatible schemas and units."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.config import settings
from app.data_providers.normalizer import normalize_daily
from app.market_time import cn_now
from app.plugins.mootdx.client import MootdxClient, MootdxError

logger = logging.getLogger(__name__)

_DATASETS = (
    "realtime",
    "daily",
    "adj_factor",
    "minute",
    "depth5",
    "financial",
    "full_minute",
)
_DAILY_FREQUENCY = 9
_MINUTE_FREQUENCIES = {"1m": 8, "5m": 0, "15m": 1, "30m": 2, "60m": 3, "1h": 3}
_PAGE_SIZE = 800
_QUOTE_BATCH = 80
_DAILY_YIELD_SYMBOLS = 20
_FINANCIAL_HISTORY_PERIODS = 8
_BJ_TDX_MARKET = 2
_BEIJING = ZoneInfo("Asia/Shanghai")
_MINUTE_COLUMNS = ["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]

_SH_STOCK_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZ_STOCK_PREFIXES = ("000", "001", "002", "003", "300", "301")
_BJ_STOCK_PREFIXES = ("4", "8", "92")

# FINVALUE IDs, not Chinese labels: upstream repeats labels for single-quarter
# and year-to-date values. Monetary fields are yuan; capital fields are shares.
_FINANCIAL_MAPS = {
    "metrics": {
        "col1": "eps_basic",
        "col4": "bps",
        "col281": "roe",
        "col202": "gross_margin",
        "col199": "net_margin",
        "col183": "revenue_yoy",
        "col184": "net_income_yoy",
        "col210": "debt_to_asset_ratio",
    },
    "income": {
        "col74": "revenue",
        "col75": "operating_cost",
        "col76": "tax_and_surcharges",
        "col77": "selling_expense",
        "col78": "admin_expense",
        "col80": "financial_expense",
        "col86": "operating_profit",
        "col92": "total_profit",
        "col93": "income_tax",
        "col95": "net_income",
        "col96": "net_income_attributable",
        "col1": "basic_eps",
    },
    "balance_sheet": {
        "col8": "cash_and_equivalents",
        "col11": "accounts_receivable",
        "col17": "inventory",
        "col21": "total_current_assets",
        "col27": "fixed_assets",
        "col33": "intangible_assets",
        "col39": "total_non_current_assets",
        "col40": "total_assets",
        "col54": "total_current_liabilities",
        "col62": "total_non_current_liabilities",
        "col63": "total_liabilities",
        "col72": "total_equity",
    },
    "cash_flow": {
        "col107": "net_operating_cash_flow",
        "col119": "net_investing_cash_flow",
        "col128": "net_financing_cash_flow",
        "col131": "net_cash_change",
        "col114": "capex",
    },
    "shares": {
        "col238": "total_shares",
        "col239": "float_shares",
    },
}
_HISTORY_COLUMNS = {column for fields in _FINANCIAL_MAPS.values() for column in fields} | {
    "col314"
}
_CACHE_TTL = 3600


@dataclass
class _MootdxConfig:
    name: str = "mootdx"
    display_name: str = "mootdx"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def _to_float(value, *, zero_is_null: bool = False) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) >= 1e30 or (zero_is_null and number == 0):
        return None
    return number


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    digits = "".join(character for character in text if character.isdigit())
    if len(digits) == 6:
        digits = f"20{digits}"
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _as_beijing_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_BEIJING).replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _symbol(code: str, exchange: str | None = None) -> str | None:
    code = str(code or "").strip().upper()
    if "." in code:
        raw, suffix = code.split(".", 1)
        return (
            f"{raw}.{suffix}"
            if suffix in {"SH", "SZ", "BJ"} and len(raw) == 6 and raw.isdigit()
            else None
        )
    if not code.isdigit() or len(code) != 6:
        return None
    if exchange:
        return f"{code}.{exchange}"
    if code.startswith(_SH_STOCK_PREFIXES):
        return f"{code}.SH"
    if code.startswith(_SZ_STOCK_PREFIXES):
        return f"{code}.SZ"
    if code.startswith(_BJ_STOCK_PREFIXES):
        return f"{code}.BJ"
    return None


def _is_a_share(code: str, exchange: str) -> bool:
    prefixes = {
        "SH": _SH_STOCK_PREFIXES,
        "SZ": _SZ_STOCK_PREFIXES,
        "BJ": _BJ_STOCK_PREFIXES,
    }[exchange]
    return len(code) == 6 and code.isdigit() and code.startswith(prefixes)


def _ref_price(
    prev_close: float,
    dividend: float,
    bonus: float,
    allot: float,
    allot_price: float,
    decimals: int = 2,
) -> float | None:
    denominator = 1.0 + bonus + allot
    if denominator <= 0:
        return None
    raw = (prev_close - dividend + allot * allot_price) / denominator
    scale = 10 ** decimals
    return math.floor(raw * scale + 0.5) / scale


def _price_limit(symbol: str) -> float:
    code = symbol.split(".", 1)[0]
    if code.startswith(("300", "301", "688", "689")):
        return 0.20
    if code.startswith(_BJ_STOCK_PREFIXES):
        return 0.30
    return 0.10


def _preview(provider: str, dataset: str, frame: pl.DataFrame) -> dict:
    rows = frame.head(5).to_dicts() if not frame.is_empty() else []
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, (date, datetime)):
                row[key] = value.isoformat()
    result = {
        "provider": provider,
        "dataset": dataset,
        "rows": frame.height,
        "columns": frame.columns,
        "preview": rows,
    }
    if frame.is_empty():
        result["error"] = (
            "未获取到数据, 请检查证券代码、时间范围和服务器连接。"
            "休市、停牌或窗口内无除权事件也可能返回空结果。"
        )
    return result


class MootdxProvider:
    """Free TDX protocol provider backed by mootdx."""

    name = "mootdx"
    builtin = True
    minute_history_days = 30

    def __init__(self, client_factory: Callable[[], MootdxClient] = MootdxClient) -> None:
        self.config = _MootdxConfig()
        self._client_factory = client_factory
        self._instrument_cache: dict[str, list[dict]] = {}
        self._instrument_cache_date: date | None = None
        self._instrument_lock = threading.Lock()
        self._finance_cache: dict[str, tuple[float, dict]] = {}
        self._finance_lock = threading.Lock()
        self._history_cache: dict[tuple[tuple[str, ...], int], tuple[float, list[dict]]] = {}
        self._history_lock = threading.Lock()

    def close(self) -> None:
        self._instrument_cache = {}
        self._instrument_cache_date = None
        self._finance_cache.clear()
        self._history_cache.clear()

    @staticmethod
    def _code(symbol: str) -> str:
        return symbol.split(".", 1)[0]

    @staticmethod
    def _workers(symbol_count: int) -> int:
        try:
            configured = int(os.getenv("MOOTDX_WORKERS", "4"))
        except ValueError:
            configured = 4
        return max(1, min(configured, 8, symbol_count))

    # ---- instruments ----
    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        if asset_type not in {"stock", "index", "etf"}:
            return []
        today = cn_now().date()
        with self._instrument_lock:
            if asset_type in self._instrument_cache and self._instrument_cache_date == today:
                return deepcopy(self._instrument_cache[asset_type])

        rows: list[dict] = []
        complete = True
        try:
            with self._client_factory() as client:
                for market, exchange in ((0, "SZ"), (1, "SH"), (_BJ_TDX_MARKET, "BJ")):
                    try:
                        market_rows = client.stocks(market)
                    except MootdxError as exc:
                        logger.warning("mootdx %s 标的列表不可用: %s", exchange, exc)
                        complete = complete and exchange == "BJ"
                        continue
                    if not market_rows and exchange != "BJ":
                        complete = False
                    for item in market_rows:
                        code = str(item.get("code") or "").strip()
                        name = str(item.get("name") or code).strip()
                        matches = _is_a_share(code, exchange)
                        if asset_type == "index":
                            matches = (
                                exchange == "SH" and code.startswith("000")
                            ) or (exchange == "SZ" and code.startswith("399"))
                        elif asset_type == "etf":
                            matches = "ETF" in name.upper() and (
                                (exchange == "SH" and code.startswith(("51", "52", "56", "58")))
                                or (exchange == "SZ" and code.startswith("15"))
                            )
                        if not matches:
                            continue
                        rows.append({
                            "symbol": f"{code}.{exchange}", "name": name, "code": code,
                            "exchange": exchange, "region": "CN", "type": asset_type,
                            "ext": {
                                "listing_date": None, "total_shares": None, "float_shares": None,
                                "tick_size": 0.01 if asset_type == "stock" else 0.001,
                                "limit_up": None, "limit_down": None,
                            },
                        })
        except MootdxError as exc:
            logger.warning("mootdx 标的列表拉取失败: %s", exc)
            return []

        if not complete:
            logger.warning("mootdx 沪深标的列表不完整, 保留原维表")
            return []
        rows = list({row["symbol"]: row for row in rows}.values())
        rows.sort(key=lambda row: row["symbol"])
        if asset_type == "stock":
            snapshots = {row["symbol"]: row for row in self._snapshot_rows(
                [row["symbol"] for row in rows]
            )}
            for row in rows:
                snapshot = snapshots.get(row["symbol"], {})
                listing_date = _as_date(snapshot.get("ipo_date"))
                row["ext"].update({
                    "listing_date": listing_date.isoformat() if listing_date else None,
                    "total_shares": _to_float(snapshot.get("zongguben"), zero_is_null=True),
                    "float_shares": _to_float(snapshot.get("liutongguben"), zero_is_null=True),
                })
        if rows and complete:
            with self._instrument_lock:
                if self._instrument_cache_date != today:
                    self._instrument_cache.clear()
                self._instrument_cache[asset_type] = rows
                self._instrument_cache_date = today
        return deepcopy(rows)

    def _name_map(self) -> dict[str, str]:
        return {
            row["symbol"]: row["name"]
            for asset_type in ("stock", "etf")
            for row in self.get_instruments(asset_type)
            if row.get("symbol") and row.get("name")
        }

    # ---- bars ----
    def _bar_records(
        self,
        client,
        symbol: str,
        frequency: int,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> list[dict]:
        end_dt = _as_beijing_datetime(end_time or cn_now())
        default_days = 365 if frequency == _DAILY_FREQUENCY else self.minute_history_days
        start_dt = _as_beijing_datetime(start_time) or (end_dt - timedelta(days=default_days))
        try:
            max_pages = max(1, min(int(os.getenv("MOOTDX_MAX_PAGES", "64")), 80))
        except ValueError:
            max_pages = 64
        records: list[dict] = []
        oldest: datetime | None = None
        for page in range(max_pages):
            batch = client.bars(
                symbol,
                frequency=frequency,
                start=page * _PAGE_SIZE,
                offset=_PAGE_SIZE,
            )
            if not batch:
                break
            parsed = [_as_beijing_datetime(row.get("datetime")) for row in batch]
            dates = [value for value in parsed if value is not None]
            if not dates or (oldest is not None and min(dates) >= oldest):
                logger.warning("mootdx %s K线分页未前进或缺少时间字段", symbol)
                break
            oldest = min(dates)
            records.extend(batch)
            if dates and min(dates) <= start_dt:
                break
            if len(batch) < _PAGE_SIZE:
                break
        return records

    @staticmethod
    def _daily_frame(
        raw: list[dict],
        symbol: str,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> pl.DataFrame:
        start_date = _as_beijing_datetime(start_time).date() if start_time else None
        end_date = _as_beijing_datetime(end_time).date() if end_time else None
        rows: list[dict] = []
        for item in raw:
            day = _as_date(item.get("datetime") or item.get("date"))
            if day is None or (start_date and day < start_date) or (end_date and day > end_date):
                continue
            volume = _to_float(item.get("volume", item.get("vol")))
            rows.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "open": _to_float(item.get("open")),
                    "high": _to_float(item.get("high")),
                    "low": _to_float(item.get("low")),
                    "close": _to_float(item.get("close")),
                    "volume": volume,
                    "amount": _to_float(item.get("amount")),
                }
            )
        if not rows:
            return pl.DataFrame()
        return (
            normalize_daily(rows, source="mootdx")
            .unique(subset=["symbol", "date"], keep="last")
            .sort(["symbol", "date"])
        )

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        valid = [symbol for symbol in symbols if _symbol(symbol) is not None]
        if not valid:
            return
        frames: list[pl.DataFrame] = []
        failures = 0
        try:
            with self._client_factory() as client:
                for index, symbol in enumerate(valid):
                    if symbol.endswith(".BJ"):
                        continue
                    try:
                        raw = self._bar_records(
                            client,
                            symbol,
                            _DAILY_FREQUENCY,
                            start_time,
                            end_time,
                        )
                        frame = self._daily_frame(raw, symbol, start_time, end_time)
                        failures = 0
                        if not frame.is_empty():
                            frames.append(frame)
                    except MootdxError as exc:
                        failures += 1
                        logger.warning("mootdx 日K %s 拉取失败: %s", symbol, exc)
                    if on_chunk_done:
                        on_chunk_done(index + 1, len(valid))
                    if failures >= 3:
                        logger.warning("mootdx 日K连续连接/协议失败, 终止本批次")
                        break
                    if len(frames) >= _DAILY_YIELD_SYMBOLS:
                        yield pl.concat(frames, how="diagonal_relaxed")
                        frames.clear()
        except MootdxError as exc:
            logger.warning("mootdx 日K连接失败: %s", exc)
        if frames:
            yield pl.concat(frames, how="diagonal_relaxed")

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        frames = list(
            self.iter_daily(
                symbols,
                start_time,
                end_time,
                asset_type=asset_type,
                on_chunk_done=on_chunk_done,
            )
        )
        return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()

    # ---- minute/full-minute ----
    @staticmethod
    def _minute_frame(
        raw: list[dict],
        symbol: str,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> pl.DataFrame:
        rows: list[dict] = []
        for item in raw:
            timestamp = _as_beijing_datetime(item.get("datetime"))
            if timestamp is None:
                continue
            if start_time and timestamp < _as_beijing_datetime(start_time):
                continue
            if end_time and timestamp > _as_beijing_datetime(end_time):
                continue
            volume = _to_float(item.get("volume", item.get("vol")))
            rows.append(
                {
                    "symbol": symbol,
                    "datetime": timestamp,
                    "open": _to_float(item.get("open")),
                    "high": _to_float(item.get("high")),
                    "low": _to_float(item.get("low")),
                    "close": _to_float(item.get("close")),
                    # TDX intraday bars use shares; frequency=9 daily and
                    # security quotes use lots. Verified against amount/VWAP.
                    "volume": volume / 100.0 if volume is not None else None,
                    "amount": _to_float(item.get("amount")),
                }
            )
        if not rows:
            return pl.DataFrame()
        frame = pl.from_dicts(rows, infer_schema_length=None)
        frame = frame.with_columns(
            pl.col("datetime").cast(pl.Datetime("us")),
            *[
                pl.col(column).cast(pl.Float64, strict=False)
                for column in ("open", "high", "low", "close", "volume", "amount")
            ],
        )
        return (
            frame.select(_MINUTE_COLUMNS)
            .unique(subset=["symbol", "datetime"], keep="last")
            .sort(["symbol", "datetime"])
        )

    def _minute_partition(
        self,
        symbols: list[str],
        frequency: int,
        start_time: datetime | None,
        end_time: datetime | None,
    ) -> list[pl.DataFrame]:
        frames: list[pl.DataFrame] = []
        failures = 0
        try:
            with self._client_factory() as client:
                for symbol in symbols:
                    if symbol.endswith(".BJ"):
                        continue
                    try:
                        raw = self._bar_records(
                            client,
                            symbol,
                            frequency,
                            start_time,
                            end_time,
                        )
                        frame = self._minute_frame(raw, symbol, start_time, end_time)
                        failures = 0
                        if not frame.is_empty():
                            frames.append(frame)
                    except MootdxError as exc:
                        failures += 1
                        logger.warning("mootdx 分钟K %s 拉取失败: %s", symbol, exc)
                    if failures >= 3:
                        logger.warning("mootdx 分钟K连续连接/协议失败, 终止本批次")
                        break
        except MootdxError as exc:
            logger.warning("mootdx 分钟K连接失败: %s", exc)
        return frames

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        valid = [symbol for symbol in symbols if _symbol(symbol) is not None]
        if not valid:
            return pl.DataFrame()
        frequency = _MINUTE_FREQUENCIES.get(str(freq).lower())
        if frequency is None:
            raise ValueError(f"mootdx 不支持分钟周期: {freq}")
        workers = self._workers(len(valid))
        partitions = [valid[index::workers] for index in range(workers)]
        frames: list[pl.DataFrame] = []
        if workers == 1:
            frames.extend(self._minute_partition(partitions[0], frequency, start_time, end_time))
            if on_chunk_done:
                on_chunk_done(1, 1)
        else:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="mootdx-minute",
            ) as pool:
                futures = [
                    pool.submit(
                        self._minute_partition,
                        partition,
                        frequency,
                        start_time,
                        end_time,
                    )
                    for partition in partitions
                ]
                for index, future in enumerate(as_completed(futures), start=1):
                    frames.extend(future.result())
                    if on_chunk_done:
                        on_chunk_done(index, len(futures))
        return (
            pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "datetime"])
            if frames
            else pl.DataFrame()
        )

    def get_intraday_batch(
        self,
        symbols: list[str],
        count: int = 300,
        asset_type: str = "stock",
    ) -> pl.DataFrame:
        end = cn_now().replace(tzinfo=None)
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
        return self.get_minute(
            symbols,
            start_time=start,
            end_time=end,
            asset_type=asset_type,
            freq="1m",
        )

    # ---- realtime/depth ----
    def _quote_rows(self, symbols: list[str], *, include_names: bool) -> list[dict]:
        supported = [
            symbol for symbol in symbols if symbol.endswith((".SH", ".SZ"))
        ]
        if not supported:
            return []
        names = self._name_map() if include_names else {}
        fetched_ms = int(time.time() * 1000)
        result: list[dict] = []
        try:
            with self._client_factory() as client:
                for start in range(0, len(supported), _QUOTE_BATCH):
                    chunk = supported[start : start + _QUOTE_BATCH]
                    try:
                        rows = client.quotes(chunk)
                    except MootdxError as exc:
                        logger.warning("mootdx 实时行情批次失败: %s", exc)
                        continue
                    for row in rows:
                        market = row.get("market")
                        exchange = "SH" if market == 1 else "SZ" if market == 0 else None
                        symbol = _symbol(str(row.get("code") or ""), exchange)
                        if symbol not in chunk:
                            continue
                        price = _to_float(row.get("price"))
                        previous = _to_float(row.get("last_close"))
                        change = (
                            price - previous
                            if price is not None and previous is not None
                            else None
                        )
                        change_pct = (
                            change / previous
                            if change is not None and previous not in (None, 0)
                            else None
                        )
                        volume = _to_float(row.get("vol"))
                        result.append(
                            {
                                "symbol": symbol,
                                "name": names.get(symbol),
                                "last_price": price,
                                "prev_close": previous,
                                "open": _to_float(row.get("open")),
                                "high": _to_float(row.get("high")),
                                "low": _to_float(row.get("low")),
                                "volume": volume,
                                "amount": _to_float(row.get("amount")),
                                "change_pct": change_pct,
                                "change_amount": change,
                                "amplitude": None,
                                "turnover_rate": None,
                                "timestamp": fetched_ms,
                                "session": None,
                            }
                        )
        except MootdxError as exc:
            logger.warning("mootdx 实时行情连接失败: %s", exc)
        return result

    def get_realtime(self) -> list[dict]:
        symbols = [
            row["symbol"] for asset_type in ("stock", "etf")
            for row in self.get_instruments(asset_type)
        ]
        return self._quote_rows(symbols, include_names=True)

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        rows = self._quote_rows(symbols, include_names=False)
        return rows or None

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        supported = [
            symbol for symbol in symbols if symbol.endswith((".SH", ".SZ"))
            and not (symbol.endswith(".SH") and symbol.startswith(("000", "880", "999")))
            and not (symbol.endswith(".SZ") and symbol.startswith("399"))
        ]
        if not supported:
            return {}
        fetched_ms = int(time.time() * 1000)
        result: dict[str, dict] = {}
        try:
            with self._client_factory() as client:
                for start in range(0, len(supported), _QUOTE_BATCH):
                    chunk = supported[start : start + _QUOTE_BATCH]
                    try:
                        rows = client.quotes(chunk)
                    except MootdxError as exc:
                        logger.warning("mootdx 五档批次失败: %s", exc)
                        continue
                    for row in rows:
                        market = row.get("market")
                        exchange = "SH" if market == 1 else "SZ" if market == 0 else None
                        symbol = _symbol(str(row.get("code") or ""), exchange)
                        if symbol not in chunk:
                            continue
                        result[symbol] = {
                            "ask_prices": [
                                _to_float(row.get(f"ask{level}"))
                                for level in range(1, 6)
                            ],
                            "ask_volumes": [
                                _to_float(row.get(f"ask_vol{level}"))
                                for level in range(1, 6)
                            ],
                            "bid_prices": [
                                _to_float(row.get(f"bid{level}"))
                                for level in range(1, 6)
                            ],
                            "bid_volumes": [
                                _to_float(row.get(f"bid_vol{level}"))
                                for level in range(1, 6)
                            ],
                            "timestamp": fetched_ms,
                        }
        except MootdxError as exc:
            logger.warning("mootdx 五档连接失败: %s", exc)
        return result

    # ---- adjustment factors ----
    def get_adj_factors(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        schema = {"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}
        valid = [symbol for symbol in symbols if _symbol(symbol) is not None]
        if not valid or asset_type not in {"stock", "etf"}:
            return pl.DataFrame(schema=schema)
        output: list[dict] = []
        try:
            with self._client_factory() as client:
                for index, symbol in enumerate(valid):
                    try:
                        events: dict[date, list[dict]] = {}
                        for row in client.xdxr(symbol):
                            if int(row.get("category") or 0) != 1:
                                continue
                            event_date = _as_date(
                                f"{int(row.get('year') or 0):04d}-"
                                f"{int(row.get('month') or 0):02d}-"
                                f"{int(row.get('day') or 0):02d}"
                            )
                            if event_date is None or event_date > cn_now().date():
                                continue
                            if start_time and event_date < start_time.date():
                                continue
                            if end_time and event_date > end_time.date():
                                continue
                            events.setdefault(event_date, []).append(row)
                        if events:
                            first = min(events) - timedelta(days=365)
                            last = max(events)
                            bars = self._bar_records(
                                client,
                                symbol,
                                _DAILY_FREQUENCY,
                                datetime.combine(first, datetime.min.time()),
                                datetime.combine(last, datetime.max.time()),
                            )
                            closes = {
                                day: close
                                for item in bars
                                if (day := _as_date(item.get("datetime"))) is not None
                                and (close := _to_float(item.get("close"))) is not None
                            }
                            days = sorted(closes)
                            for event_date, event_rows in events.items():
                                previous_days = [day for day in days if day < event_date]
                                if not previous_days:
                                    continue
                                previous = closes[previous_days[-1]]
                                if any(
                                    _to_float(row.get("peigu"))
                                    and _to_float(row.get("peigujia")) is None
                                    for row in event_rows
                                ):
                                    logger.warning("mootdx %s 配股缺少配股价, 跳过因子", symbol)
                                    continue
                                dividend = sum(
                                    _to_float(row.get("fenhong")) or 0 for row in event_rows
                                ) / 10
                                bonus = sum(
                                    _to_float(row.get("songzhuangu")) or 0 for row in event_rows
                                ) / 10
                                allot = sum(
                                    _to_float(row.get("peigu")) or 0 for row in event_rows
                                ) / 10
                                allot_value = sum(
                                    (_to_float(row.get("peigu")) or 0)
                                    * (_to_float(row.get("peigujia")) or 0)
                                    for row in event_rows
                                ) / 10
                                reference = _ref_price(
                                    previous,
                                    dividend,
                                    bonus,
                                    allot,
                                    allot_value / allot if allot else 0.0,
                                    decimals=3 if asset_type == "etf" else 2,
                                )
                                if reference is None or reference <= 0:
                                    continue
                                factor = previous / reference
                                ex_days = [day for day in days if day >= event_date]
                                if ex_days:
                                    adjusted_return = closes[ex_days[0]] / reference - 1.0
                                    limit = 0.20 if asset_type == "etf" else _price_limit(symbol)
                                    if abs(adjusted_return) > limit + 0.02:
                                        logger.warning(
                                            "mootdx 除权因子自检剔除 %s %s: %.2f%%",
                                            symbol,
                                            event_date,
                                            adjusted_return * 100,
                                        )
                                        continue
                                output.append(
                                    {
                                        "symbol": symbol,
                                        "trade_date": event_date,
                                        "ex_factor": factor,
                                    }
                                )
                    except MootdxError as exc:
                        logger.warning("mootdx 除权因子 %s 拉取失败: %s", symbol, exc)
                    if on_chunk_done:
                        on_chunk_done(index + 1, len(valid))
        except MootdxError as exc:
            logger.warning("mootdx 除权因子连接失败: %s", exc)
        if not output:
            return pl.DataFrame(schema=schema)
        return (
            pl.from_dicts(output, schema=schema)
            .unique(subset=["symbol", "trade_date"], keep="last")
            .sort(["symbol", "trade_date"])
        )

    # ---- financials ----
    def _history_rows(self, symbols: list[str], periods: int) -> list[dict]:
        key = (tuple(sorted(symbols)), periods)
        with self._history_lock:
            cached = self._history_cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < _CACHE_TTL:
            return cached[1]
        try:
            rows = self._client_factory().financial_history(
                symbols,
                periods=periods,
                columns=_HISTORY_COLUMNS,
                cache_dir=Path(settings.data_dir) / "cache" / "mootdx" / "financial",
            )
        except MootdxError as exc:
            logger.warning("mootdx 历史财务不可用: %s", exc)
            rows = []
        if rows:
            with self._history_lock:
                if len(self._history_cache) >= 16:
                    self._history_cache.pop(next(iter(self._history_cache)))
                self._history_cache[key] = (time.monotonic(), rows)
        return rows

    def _snapshot_rows(self, symbols: list[str]) -> list[dict]:
        now = time.monotonic()
        with self._finance_lock:
            found = {
                symbol: cached[1] for symbol in symbols
                if (cached := self._finance_cache.get(symbol)) is not None
                and now - cached[0] < _CACHE_TTL
            }
        missing = [symbol for symbol in symbols if symbol not in found]
        if missing:
            workers = self._workers(len(missing))
            partitions = [missing[index::workers] for index in range(workers)]
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mootdx-meta") as pool:
                for rows in pool.map(self._snapshot_partition, partitions):
                    found.update(rows)
            with self._finance_lock:
                for symbol in missing:
                    if symbol in found:
                        self._finance_cache[symbol] = (time.monotonic(), found[symbol])
        return [dict(found[symbol], symbol=symbol) for symbol in symbols if symbol in found]

    def _snapshot_partition(self, symbols: list[str]) -> dict[str, dict]:
        found = {}
        failures = 0
        try:
            with self._client_factory() as client:
                for symbol in symbols:
                    if symbol.endswith(".BJ"):
                        continue
                    try:
                        rows = client.finance(symbol)
                        if rows:
                            found[symbol] = rows[0]
                            failures = 0
                        else:
                            failures += 1
                    except MootdxError as exc:
                        failures += 1
                        logger.warning("mootdx 股本快照 %s 失败: %s", symbol, exc)
                    if failures >= 3:
                        logger.warning("mootdx 股本快照连续失败, 终止本批次")
                        break
        except MootdxError as exc:
            logger.warning("mootdx 股本快照连接失败: %s", exc)
        return found

    @staticmethod
    def _map_history(table: str, rows: list[dict]) -> pl.DataFrame:
        field_map = _FINANCIAL_MAPS[table]
        output: list[dict] = []
        for row in rows:
            symbol = _symbol(str(row.get("code") or ""))
            period_end = _as_date(row.get("report_date"))
            announce = _as_date(row.get("col314"))
            # Unknown publication dates cannot safely enter the historical
            # shares as-of join (legacy readers fall back to period_end).
            if (
                symbol is None or period_end is None or announce is None
                or announce < period_end or announce > cn_now().date()
            ):
                continue
            mapped = {
                "symbol": symbol,
                "period_end": period_end.isoformat(),
                "announce_date": announce.isoformat(),
            }
            for upstream, canonical in field_map.items():
                mapped[canonical] = _to_float(row.get(upstream))
            if any(mapped[column] is not None for column in field_map.values()):
                output.append(mapped)
        return MootdxProvider._financial_frame(output)

    @staticmethod
    def _financial_frame(rows: list[dict]) -> pl.DataFrame:
        if not rows:
            return pl.DataFrame()
        frame = pl.from_dicts(rows, infer_schema_length=None)
        for column in frame.columns:
            if column not in {"symbol", "period_end", "announce_date"}:
                frame = frame.with_columns(pl.col(column).cast(pl.Float64, strict=False))
        return frame.sort(["symbol", "period_end"])

    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        if table not in _FINANCIAL_MAPS:
            raise ValueError(f"mootdx 不支持财务表: {table}")
        valid = [symbol for symbol in symbols if _symbol(symbol) is not None]
        if not valid:
            return pl.DataFrame()
        periods = 4 if latest_only else _FINANCIAL_HISTORY_PERIODS
        frame = self._map_history(table, self._history_rows(valid, periods))
        if not frame.is_empty():
            frame = frame.filter(pl.col("symbol").is_in(valid))
        if latest_only and not frame.is_empty():
            frame = (
                frame.sort(["symbol", "period_end"])
                .group_by("symbol", maintain_order=True)
                .tail(1)
                .sort("symbol")
            )
        return frame

    # ---- settings test pull ----
    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        selected = [symbol for symbol in (symbols or ["600519.SH"]) if _symbol(symbol)]
        end = cn_now().replace(tzinfo=None)
        try:
            if dataset == "daily":
                return _preview(
                    self.name,
                    dataset,
                    self.get_daily(selected, end - timedelta(days=30), end),
                )
            if dataset == "adj_factor":
                return _preview(
                    self.name,
                    dataset,
                    self.get_adj_factors(selected, end - timedelta(days=365), end),
                )
            if dataset in {"minute", "full_minute"}:
                return _preview(
                    self.name,
                    dataset,
                    self.get_minute(selected, end - timedelta(days=14), end),
                )
            if dataset == "financial":
                return _preview(
                    self.name,
                    dataset,
                    self.get_financials("metrics", selected[:1], latest_only=True),
                )
            if dataset == "depth5":
                depth = self.get_depth_batch(selected)
                frame = pl.from_dicts(
                    [{"symbol": symbol, **row} for symbol, row in depth.items()],
                    infer_schema_length=None,
                ) if depth else pl.DataFrame()
                return _preview(self.name, dataset, frame)
            if dataset == "realtime":
                rows = self._quote_rows(selected, include_names=False)
                frame = pl.from_dicts(rows, infer_schema_length=None) if rows else pl.DataFrame()
                return _preview(self.name, dataset, frame)
        except (MootdxError, ValueError) as exc:
            return {"provider": self.name, "dataset": dataset, "rows": 0, "error": str(exc)}
        raise ValueError(f"mootdx 不支持数据集: {dataset}")
