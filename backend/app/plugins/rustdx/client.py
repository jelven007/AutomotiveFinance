"""Failure-aware Python facade for the native rustdx connection pool."""

from __future__ import annotations

import importlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from app.plugins.mootdx.client import MootdxError

_RUSTDX_CRATE_VERSION = "1.11.0"
_MAX_CONNECTIONS = 35
_DEFAULT_SERVER = "117.34.114.13:7709"
_NATIVE_LOCK = threading.Lock()
_NATIVE_CLIENT = None


class RustdxError(MootdxError):
    """rustdx protocol, bridge, or connection failure."""


def _connection_count() -> int:
    try:
        configured = int(os.getenv("RUSTDX_QUOTE_CONNECTIONS", str(_MAX_CONNECTIONS)))
    except ValueError:
        configured = _MAX_CONNECTIONS
    return max(1, min(configured, _MAX_CONNECTIONS))


def _timeout_seconds() -> int:
    try:
        configured = int(os.getenv("RUSTDX_TIMEOUT", "8"))
    except ValueError:
        configured = 8
    return max(2, min(configured, 60))


def _native_client():
    global _NATIVE_CLIENT
    with _NATIVE_LOCK:
        if _NATIVE_CLIENT is None:
            module = importlib.import_module("tsp_rustdx_native")
            server = os.getenv("RUSTDX_SERVER", _DEFAULT_SERVER).strip() or _DEFAULT_SERVER
            _NATIVE_CLIENT = module.RustdxClient(
                _connection_count(),
                server,
                _timeout_seconds(),
            )
        return _NATIVE_CLIENT


def availability() -> tuple[bool, str]:
    """Plugin check used by the loader without opening a network connection."""
    try:
        module = importlib.import_module("tsp_rustdx_native")
        maximum = int(module.MAX_CONNECTIONS)
        version = str(module.__version__)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        return False, f"缺少 rustdx 原生桥接: {exc}"
    if maximum != _MAX_CONNECTIONS:
        return False, f"rustdx 原生桥接连接上限异常: {maximum}"
    return True, f"rustdx-complete {_RUSTDX_CRATE_VERSION} / bridge {version}"


def close_native_pool() -> None:
    global _NATIVE_CLIENT
    with _NATIVE_LOCK:
        if _NATIVE_CLIENT is not None:
            _NATIVE_CLIENT.close()
            _NATIVE_CLIENT = None


def _security(symbol: str) -> tuple[int, str]:
    code, separator, exchange = str(symbol or "").upper().partition(".")
    if len(code) != 6 or not code.isdigit():
        raise RustdxError(f"无效证券代码: {symbol}")
    if not separator:
        exchange = "SH" if code.startswith("6") else "SZ" if code.startswith(("0", "3")) else ""
    if exchange == "BJ":
        raise RustdxError("rustdx 当前标准行情接口不支持北交所")
    if exchange not in {"SH", "SZ"}:
        raise RustdxError(f"无效交易所: {symbol}")
    return (1 if exchange == "SH" else 0), code


def _is_index(market: int, code: str) -> bool:
    return (market == 1 and code.startswith(("000", "880", "999"))) or (
        market == 0 and code.startswith("399")
    )


def _records(payload: str) -> list[dict[str, Any]]:
    try:
        rows = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise RustdxError(f"rustdx 返回无效 JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise RustdxError("rustdx 返回结构不是数组")
    return [dict(row) for row in rows if isinstance(row, dict)]


def _datetime_text(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    try:
        year = int(value["year"])
        month = int(value["month"])
        day = int(value["day"])
        hour = int(value.get("hour", 15))
        minute = int(value.get("minute", 0))
    except (KeyError, TypeError, ValueError):
        return None
    return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"


class RustdxClient:
    """One facade over a process-wide native pool capped at 35 connections."""

    def __enter__(self) -> RustdxClient:
        _native_client()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def close(self) -> None:
        """Individual facades do not own the shared native pool."""

    @staticmethod
    def close_shared() -> None:
        close_native_pool()

    @property
    def max_connections(self) -> int:
        return int(_native_client().max_connections)

    def stats(self) -> dict[str, Any]:
        return dict(json.loads(_native_client().stats_json()))

    def quotes(self, codes: list[str]) -> list[dict]:
        if not codes:
            return []
        try:
            securities = [_security(code) for code in codes]
            rows = _records(_native_client().quotes_json(securities))
        except RustdxError:
            raise
        except Exception as exc:
            raise RustdxError(f"rustdx quotes 调用失败: {exc}") from exc
        for row in rows:
            for side in ("bid", "ask"):
                for level in range(1, 6):
                    field = f"{side}{level}_vol"
                    if field in row:
                        row[f"{side}_vol{level}"] = row.pop(field)
            for field in ("vol", "amount"):
                if row.get(field) == 2.0**-127:
                    row[field] = 0.0
            if row.get("market") == 1 and str(row.get("code", "")).startswith(
                ("52", "53", "56", "58", "59")
            ):
                for field in (
                    "price",
                    "last_close",
                    "open",
                    "high",
                    "low",
                    *(f"{side}{level}" for side in ("bid", "ask") for level in range(1, 6)),
                ):
                    if row.get(field) is not None:
                        row[field] /= 10.0
        return rows

    def bars(
        self,
        code: str,
        *,
        frequency: int,
        start: int = 0,
        offset: int = 800,
    ) -> list[dict]:
        market, raw_code = _security(code)
        index = _is_index(market, raw_code)
        wire_frequency = 4 if index and frequency == 9 else frequency
        try:
            rows = _records(
                _native_client().bars_json(
                    market,
                    raw_code,
                    wire_frequency,
                    start,
                    min(max(offset, 1), 800),
                    index,
                )
            )
        except Exception as exc:
            raise RustdxError(f"rustdx bars 调用失败: {exc}") from exc
        for row in rows:
            row["datetime"] = _datetime_text(row.pop("dt", None))
            for field in ("vol", "amount"):
                if row.get(field) == 2.0**-127:
                    row[field] = 0.0
            if index and frequency in {0, 1, 2, 3, 7, 8}:
                row["vol"] = None
        return rows

    def stocks(self, market: int) -> list[dict]:
        if market not in {0, 1}:
            raise RustdxError(f"rustdx 不支持市场代码: {market}")
        try:
            return _records(_native_client().stocks_json(market))
        except Exception as exc:
            raise RustdxError(f"rustdx stocks 调用失败: {exc}") from exc

    def xdxr(self, code: str) -> list[dict]:
        market, raw_code = _security(code)
        try:
            return _records(_native_client().xdxr_json(market, raw_code))
        except Exception as exc:
            raise RustdxError(f"rustdx xdxr 调用失败: {exc}") from exc

    def finance(self, code: str) -> list[dict]:
        market, raw_code = _security(code)
        try:
            return _records(_native_client().finance_json(market, raw_code))
        except Exception as exc:
            raise RustdxError(f"rustdx finance 调用失败: {exc}") from exc

    def company_info(self, code: str, category_name: str) -> str | None:
        market, raw_code = _security(code)
        try:
            return _native_client().company_info(market, raw_code, category_name)
        except Exception as exc:
            raise RustdxError(f"rustdx F10 调用失败: {exc}") from exc

    @staticmethod
    def financial_history(
        symbols: list[str],
        *,
        periods: int,
        columns: set[str],
        cache_dir: Path,
    ) -> list[dict]:
        from app.plugins.rustdx.financial import financial_history

        class ReportTransport:
            def report_file(self, filename, max_bytes):
                server = os.getenv("RUSTDX_FINANCIAL_SERVER", "120.76.152.87:7709").strip()
                return _native_client().report_file(
                    filename,
                    max_bytes,
                    server or "120.76.152.87:7709",
                )

        try:
            return financial_history(
                ReportTransport(),
                symbols,
                periods=periods,
                columns=columns,
                cache_dir=cache_dir,
            )
        except Exception as exc:
            raise RustdxError(f"rustdx 历史财务读取失败: {exc}") from exc
