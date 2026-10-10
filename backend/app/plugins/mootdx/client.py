"""Thin, failure-aware wrapper around mootdx's public TDX protocol client."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.metadata
import ipaddress
import logging
import os
import re
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from app.market_time import cn_now

logger = logging.getLogger(__name__)

_REPORT_FILE_RE = re.compile(r"^gpcw(\d{8})\.zip$")
_CONNECT_LOCK = threading.Lock()  # mootdx Quotes.factory writes its global server config
_ARCHIVE_LOCK = threading.Lock()
_DEFAULT_SERVER = ("117.34.114.13", 7709)


class MootdxError(RuntimeError):
    """mootdx connection or protocol failure."""


def availability() -> tuple[bool, str]:
    """Plugin check used by the loader. Do not make a network call at startup."""
    try:
        importlib.import_module("mootdx.quotes")
        importlib.import_module("tdxpy")
        version = importlib.metadata.version("mootdx")
    except (ImportError, importlib.metadata.PackageNotFoundError) as exc:
        return False, f"缺少 Python 依赖: {exc}"
    return True, f"mootdx {version}"


def _server_from_env(key: str = "MOOTDX_SERVER") -> tuple[str, int] | None:
    value = os.getenv(key, "").strip()
    if not value:
        return None
    try:
        host, port_text = value.rsplit(":", 1)
        ipaddress.ip_address(host)
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise MootdxError(f"{key} 格式应为 IP:PORT") from exc
    return host, port


def _timeout_from_env() -> int:
    try:
        return max(2, min(int(os.getenv("MOOTDX_TIMEOUT", "8")), 60))
    except ValueError:
        return 8


def _records(data: Any) -> list[dict]:
    if data is None:
        return []
    if isinstance(data, list):
        return [dict(row) for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [dict(data)]
    to_dict = getattr(data, "to_dict", None)
    if callable(to_dict):
        try:
            return [dict(row) for row in to_dict(orient="records")]
        except TypeError:
            pass
    return []


def _security(symbol: str) -> tuple[int, str]:
    """Keep the explicit exchange: 000001.SH and 000001.SZ are different assets."""
    code, _, exchange = symbol.upper().partition(".")
    if len(code) != 6 or not code.isdigit():
        raise MootdxError(f"无效证券代码: {symbol}")
    if exchange == "BJ":
        raise MootdxError("mootdx/tdxpy 当前标准行情接口不支持北交所")
    if exchange and exchange not in {"SH", "SZ"}:
        raise MootdxError(f"无效交易所: {symbol}")
    market = {"SH": 1, "SZ": 0}.get(exchange)
    if market is None:
        from mootdx.utils import get_stock_market

        market = get_stock_market(code)
    return market, code


def _is_index(market: int, code: str) -> bool:
    return (market == 1 and code.startswith(("000", "880", "999"))) or (
        market == 0 and code.startswith("399")
    )


def _download_report(api, filename: str, target, *, max_bytes: int) -> int:
    """Bounded streaming avoids tdxpy's filesize preallocation/append bug."""
    downloaded = 0
    while downloaded < max_bytes:
        response = api.get_report_file(f"tdxfin/{filename}", downloaded) or {}
        chunk = response.get("chunkdata") or b""
        size = response.get("chunksize", 0)
        if not size:
            return downloaded
        if size != len(chunk) or downloaded + size > max_bytes:
            raise MootdxError(f"财务文件长度异常: {filename}")
        target.write(chunk)
        downloaded += size
    return downloaded


def _verified_archive(path: Path, size: int, checksum: str) -> bool:
    if not path.exists() or path.stat().st_size != size:
        return False
    with path.open("rb") as source:
        return hashlib.file_digest(source, "md5").hexdigest() == checksum


class MootdxClient:
    """One TDX socket session.

    Provider operations create short-lived instances. This avoids sharing tdxpy's
    stateful socket between background jobs and lets full-minute workers own their
    connection independently.
    """

    def __init__(
        self,
        *,
        server: tuple[str, int] | None = None,
        timeout: int | None = None,
    ) -> None:
        self.server = server if server is not None else (_server_from_env() or _DEFAULT_SERVER)
        self.timeout = timeout or _timeout_from_env()
        self._quotes = None

    def __enter__(self) -> MootdxClient:
        self._connect()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _connect(self) -> None:
        self.close()
        try:
            from mootdx.quotes import Quotes

            kwargs: dict[str, Any] = {
                "market": "std",
                "bestip": False,
                "timeout": self.timeout,
                "heartbeat": False,
                "auto_retry": False,
                "raise_exception": True,
            }
            if self.server is not None:
                kwargs["server"] = self.server
            with _CONNECT_LOCK:
                self._quotes = Quotes.factory(**kwargs)
            if self._is_closed():
                raise MootdxError("通达信行情服务器连接失败")
        except MootdxError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise MootdxError(f"通达信行情服务器连接失败: {exc}") from exc

    def close(self) -> None:
        quotes, self._quotes = self._quotes, None
        if quotes is not None:
            with contextlib.suppress(Exception):
                quotes.close()

    def _is_closed(self) -> bool:
        if self._quotes is None:
            return True
        try:
            return bool(self._quotes.closed)
        except Exception:
            return True

    def _invoke(self, method: str, *args, client_method: bool = False, **kwargs):
        last_error: Exception | None = None
        for attempt in range(2):
            if self._quotes is None or self._is_closed():
                self._connect()
            try:
                target = self._quotes.client if client_method else self._quotes
                result = getattr(target, method)(*args, **kwargs)
                if result is None and self._is_closed():
                    raise MootdxError("连接已断开")
                return result
            except Exception as exc:
                last_error = getattr(exc, "original_exception", exc)
                if attempt == 0:
                    try:
                        self._connect()
                    except Exception as reconnect_error:
                        last_error = reconnect_error
                        break
        raise MootdxError(f"mootdx {method} 调用失败: {last_error}") from last_error

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
        # TDX index frequency=9 rounds volume in ten-thousand shares.
        # Frequency=4 has the same raw daily prices with volume in lots.
        wire_frequency = 4 if index and frequency == 9 else frequency
        rows = _records(
            self._invoke(
                "get_index_bars" if index else "get_security_bars",
                wire_frequency, market, raw_code, start, min(max(offset, 1), 800),
                client_method=True,
            )
        )
        # tdxpy get_volume(0) returns 2**-127 rather than zero. This is a
        # decoder sentinel, not a heuristic magnitude-based unit conversion.
        for row in rows:
            for field in ("vol", "amount"):
                if row.get(field) == 2.0 ** -127:
                    row[field] = 0.0
            if index and frequency in {0, 1, 2, 3, 7, 8}:
                # Index intraday VOL is amount/100 on this protocol, not shares.
                # Do not feed it into volume indicators or a synthetic VWAP.
                row["vol"] = None
        return rows

    def quotes(self, codes: list[str]) -> list[dict]:
        rows = _records(
            self._invoke("get_security_quotes", [_security(code) for code in codes],
                         client_method=True)
        ) if codes else []
        for row in rows:
            # tdxpy 0.2.7 recognizes SH funds only under 50/51; new ETF
            # prefixes fall back to 0.01 instead of 0.001 (upstream issue #159).
            if row.get("market") == 1 and str(row.get("code", "")).startswith(
                ("52", "53", "56", "58", "59")
            ):
                for field in (
                    "price", "last_close", "open", "high", "low",
                    *(f"{side}{level}" for side in ("bid", "ask") for level in range(1, 6)),
                ):
                    if row.get(field) is not None:
                        row[field] /= 10.0
        return rows

    def xdxr(self, code: str) -> list[dict]:
        return _records(self._invoke("get_xdxr_info", *_security(code), client_method=True))

    def finance(self, code: str) -> list[dict]:
        return _records(self._invoke("get_finance_info", *_security(code), client_method=True))

    def company_info(self, code: str, category_name: str) -> str | None:
        """Read one TDX F10 category by its display name."""
        market, raw_code = _security(code)
        categories = self._invoke(
            "get_company_info_category",
            market,
            raw_code,
            client_method=True,
        ) or []
        category = next(
            (
                item
                for item in categories
                if str(item.get("name") or "").strip() == category_name
            ),
            None,
        )
        if category is None:
            return None
        content = self._invoke(
            "get_company_info_content",
            market=market,
            code=raw_code,
            filename=category["filename"],
            start=category["start"],
            length=category["length"],
            client_method=True,
        )
        return str(content) if content is not None else None

    def stocks(self, market: int) -> list[dict]:
        """Read a complete market list without mootdx's progress-bar wrapper."""
        if market == 2:
            raise MootdxError("mootdx/tdxpy 当前标准行情接口不支持北交所标的列表")
        count = self._invoke("stock_count", market=market)
        try:
            count = int(count or 0)
        except (TypeError, ValueError):
            return []
        rows: list[dict] = []
        for start in range(0, min(count, 100_000), 1000):
            page = _records(
                self._invoke(
                    "get_security_list",
                    market=market,
                    start=start,
                    client_method=True,
                )
            )
            if not page:
                break
            rows.extend(page)
        if len(rows) != count or len({row.get("code") for row in rows}) != count:
            raise MootdxError(f"通达信标的列表不完整: market={market}, {len(rows)}/{count}")
        return rows

    @staticmethod
    def financial_history(
        symbols: list[str],
        *,
        periods: int,
        columns: set[str],
        cache_dir: Path,
    ) -> list[dict]:
        """Download recent official TDX financial archives and keep selected rows/columns."""
        if not symbols or periods <= 0:
            return []
        try:
            return MootdxClient._financial_history(symbols, periods, columns, cache_dir)
        except Exception as exc:
            raise MootdxError(f"mootdx 历史财务读取失败: {exc}") from exc

    @staticmethod
    def _financial_history(symbols, periods, columns, cache_dir) -> list[dict]:
        import io

        from mootdx.affair import Affair
        from mootdx.financial.financial import TdxHq_API

        server = (
            _server_from_env("MOOTDX_FINANCIAL_SERVER")
            or _server_from_env()
            or _DEFAULT_SERVER
        )
        api = TdxHq_API(auto_retry=False, raise_exception=True)
        api.need_setup = False
        records: list[dict] = []
        try:
            api.connect(*server, time_out=_timeout_from_env())
            listing = io.BytesIO()
            _download_report(api, "gpcw.txt", listing, max_bytes=512_000)
            selected = []
            today = cn_now().date()
            for line in listing.getvalue().decode("utf-8").splitlines():
                filename, checksum, size_text = line.strip().split(",")
                match = _REPORT_FILE_RE.fullmatch(filename)
                if not match or not re.fullmatch("[0-9a-f]{32}", checksum):
                    continue
                report_date = datetime.strptime(match[1], "%Y%m%d").date()
                size = int(size_text)
                # TDX publishes tiny empty archives for future report periods.
                if report_date <= today and 200 < size <= 128_000_000:
                    selected.append((match[1], filename, checksum, size))
            cache_dir.mkdir(parents=True, exist_ok=True)
            codes = {symbol.split(".", 1)[0] for symbol in symbols}
            wanted = set(columns) | {"report_date"}
            for report_date, filename, checksum, size in sorted(selected, reverse=True)[:periods]:
                path = cache_dir / filename
                with _ARCHIVE_LOCK:
                    if not _verified_archive(path, size, checksum):
                        temporary = None
                        try:
                            with tempfile.NamedTemporaryFile(dir=cache_dir, delete=False) as out:
                                temporary = Path(out.name)
                                _download_report(api, filename, out, max_bytes=size)
                            if not _verified_archive(temporary, size, checksum):
                                raise MootdxError(f"财务文件校验失败: {filename}")
                            os.replace(temporary, path)
                        finally:
                            if temporary is not None:
                                temporary.unlink(missing_ok=True)
                # Parse an isolated .dat file: avoid upstream unpack_archive's
                # unbounded extraction and temporary-directory lifecycle.
                with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as tmp:
                    members = archive.infolist()
                    if (
                        len(members) != 1 or not members[0].filename.endswith(".dat")
                        or members[0].file_size > 256_000_000
                    ):
                        raise MootdxError(f"财务压缩包结构异常: {filename}")
                    dat = Path(tmp) / "report.dat"
                    with archive.open(members[0]) as source, dat.open("wb") as out:
                        import shutil

                        shutil.copyfileobj(source, out)
                    frame = Affair.parse(downdir=tmp, filename=dat.name, header="en")
                if frame is None or frame.empty:
                    continue
                index_codes = frame.index.astype(str).str.zfill(6)
                subset = frame.loc[index_codes.isin(codes)]
                if subset.empty:
                    continue
                keep = [column for column in wanted if column in subset.columns]
                subset = subset.loc[:, keep].copy()
                subset.insert(0, "code", subset.index.astype(str).str.zfill(6))
                subset["report_date"] = report_date
                records.extend(subset.to_dict(orient="records"))
        finally:
            api.close()
        return records
