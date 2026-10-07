"""Process-local request correlation and low-cardinality HTTP metrics."""
from __future__ import annotations

import logging
import re
import threading
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_KNOWN_METHODS = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def current_request_id() -> str:
    """Return the request ID bound to the current execution context."""
    return _request_id.get()


def install_log_record_factory() -> None:
    """Inject ``request_id`` into every log record without changing call sites."""
    previous_factory = logging.getLogRecordFactory()
    if getattr(previous_factory, "_tsp_request_id_factory", False):
        return

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        record.request_id = current_request_id()
        return record

    record_factory._tsp_request_id_factory = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(record_factory)


def _safe_request_id(value: str | None) -> str:
    if value is not None and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return uuid4().hex


def _status_class(status_code: int) -> str:
    if 100 <= status_code <= 599:
        return f"{status_code // 100}xx"
    return "other"


def _normalized_method(scope: Scope) -> str:
    method = str(scope.get("method") or "").upper()
    return method if method in _KNOWN_METHODS else "OTHER"


def _empty_status_classes() -> dict[str, int]:
    return {f"{value}xx": 0 for value in range(1, 6)}


class _HttpMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._started_at = datetime.now(UTC)
            self._started_monotonic = time.monotonic()
            self._in_flight = 0
            self._totals = self._new_entry()
            self._routes: dict[tuple[str, str], dict[str, Any]] = {}

    @staticmethod
    def _new_entry() -> dict[str, Any]:
        return {
            "requests": 0,
            "errors": 0,
            "status_classes": _empty_status_classes(),
            "duration_sum_ms": 0.0,
            "duration_max_ms": 0.0,
        }

    def begin(self) -> None:
        with self._lock:
            self._in_flight += 1

    def finish(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            route_entry = self._routes.setdefault((method, route), self._new_entry())
            self._record(self._totals, status_code, duration_ms)
            self._record(route_entry, status_code, duration_ms)

    @staticmethod
    def _record(entry: dict[str, Any], status_code: int, duration_ms: float) -> None:
        entry["requests"] += 1
        if status_code >= 500:
            entry["errors"] += 1
        status_class = _status_class(status_code)
        entry["status_classes"].setdefault(status_class, 0)
        entry["status_classes"][status_class] += 1
        entry["duration_sum_ms"] += duration_ms
        entry["duration_max_ms"] = max(entry["duration_max_ms"], duration_ms)

    @staticmethod
    def _serialize_entry(entry: dict[str, Any]) -> dict[str, Any]:
        requests = int(entry["requests"])
        duration_sum_ms = float(entry["duration_sum_ms"])
        return {
            "requests": requests,
            "errors": int(entry["errors"]),
            "status_classes": dict(entry["status_classes"]),
            "duration_ms": {
                "sum": round(duration_sum_ms, 3),
                "max": round(float(entry["duration_max_ms"]), 3),
                "average": round(duration_sum_ms / requests, 3) if requests else 0.0,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            routes = [
                {
                    "method": method,
                    "route": route,
                    **self._serialize_entry(entry),
                }
                for (method, route), entry in sorted(self._routes.items())
            ]
            return {
                "process": {
                    "started_at": self._started_at.isoformat(timespec="seconds").replace(
                        "+00:00", "Z"
                    ),
                    "uptime_seconds": round(
                        max(0.0, time.monotonic() - self._started_monotonic),
                        3,
                    ),
                },
                "in_flight": self._in_flight,
                "totals": self._serialize_entry(self._totals),
                "routes": routes,
            }


_http_metrics = _HttpMetrics()


def http_metrics_snapshot() -> dict[str, Any]:
    """Return a stable snapshot that contains no URLs, queries, or headers."""
    return _http_metrics.snapshot()


def reset_http_metrics() -> None:
    """Reset process-local counters. Intended for tests and controlled diagnostics."""
    _http_metrics.reset()


async def request_id_server_error(
    request: Request,
    _exc: Exception,
) -> PlainTextResponse:
    """Preserve Starlette's default 500 response while exposing correlation."""
    request_id = getattr(request.state, "request_id", None)
    headers = (
        {_REQUEST_ID_HEADER: request_id}
        if isinstance(request_id, str) and request_id
        else None
    )
    return PlainTextResponse(
        "Internal Server Error",
        status_code=500,
        headers=headers,
    )


def _normalized_route(scope: Scope, router: Any) -> str:
    route = scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path

    partial_path: str | None = None
    for candidate in getattr(router, "routes", ()):
        try:
            match, _ = candidate.matches(scope)
        except Exception:  # pragma: no cover - defensive for third-party routes
            continue
        candidate_path = getattr(candidate, "path", None)
        if not isinstance(candidate_path, str) or not candidate_path:
            continue
        if match == Match.FULL:
            return candidate_path
        if match == Match.PARTIAL and partial_path is None:
            partial_path = candidate_path
    if partial_path is not None:
        return partial_path

    path = str(scope.get("path") or "")
    if path.startswith("/api/"):
        return "/api/{unmatched}"
    if path.startswith("/health"):
        return "/health/{unmatched}"
    return "/{unmatched}"


class ObservabilityMiddleware:
    """Correlate requests and record bounded-cardinality HTTP metrics."""

    def __init__(self, app: ASGIApp, *, router: Any) -> None:
        self.app = app
        self.router = router

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _safe_request_id(Headers(scope=scope).get(_REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id
        context_token = _request_id.set(request_id)
        started = time.perf_counter()
        status_code = 500
        _http_metrics.begin()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)[_REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "Unhandled HTTP request exception method=%s route=%s",
                _normalized_method(scope),
                _normalized_route(scope, self.router),
            )
            raise
        finally:
            _http_metrics.finish(
                method=_normalized_method(scope),
                route=_normalized_route(scope, self.router),
                status_code=status_code,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
            _request_id.reset(context_token)
