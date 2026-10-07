from __future__ import annotations

import logging
import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.services.observability import (
    ObservabilityMiddleware,
    http_metrics_snapshot,
    install_log_record_factory,
    request_id_server_error,
    reset_http_metrics,
)


@pytest.fixture(autouse=True)
def isolated_http_metrics() -> Iterator[None]:
    install_log_record_factory()
    reset_http_metrics()
    yield
    reset_http_metrics()


def _instrumented_app() -> FastAPI:
    app = FastAPI(exception_handlers={Exception: request_id_server_error})
    app.add_middleware(ObservabilityMiddleware, router=app.router)
    return app


def test_request_id_is_preserved_or_regenerated() -> None:
    app = _instrumented_app()

    @app.get("/request-id")
    def request_id(request: Request) -> dict[str, str]:
        return {"request_id": request.state.request_id}

    client = TestClient(app)
    valid = client.get("/request-id", headers={"X-Request-ID": "caller.123:abc"})
    invalid = client.get("/request-id", headers={"X-Request-ID": "invalid request id"})

    assert valid.headers["X-Request-ID"] == "caller.123:abc"
    assert valid.json()["request_id"] == "caller.123:abc"
    generated = invalid.headers["X-Request-ID"]
    assert generated == invalid.json()["request_id"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)


def test_log_records_receive_request_context(caplog: pytest.LogCaptureFixture) -> None:
    app = _instrumented_app()
    endpoint_logger = logging.getLogger("tests.observability")

    @app.get("/logged")
    def logged() -> dict[str, bool]:
        endpoint_logger.info("request handled")
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="tests.observability"):
        response = TestClient(app).get(
            "/logged",
            headers={"X-Request-ID": "trace-456"},
        )

    assert response.status_code == 200
    record = next(record for record in caplog.records if record.message == "request handled")
    assert record.request_id == "trace-456"


def test_metrics_use_route_templates_and_exclude_query_values() -> None:
    app = _instrumented_app()

    @app.get("/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    client = TestClient(app)
    assert client.get("/items/item-alpha-123?token=query-secret-one").status_code == 200
    assert client.get("/items/item-beta-456?token=query-secret-two").status_code == 200
    assert client.request("CUSTOM-ONE", "/items/ignored").status_code == 405
    assert client.request("CUSTOM-TWO", "/items/ignored").status_code == 405

    snapshot = http_metrics_snapshot()
    route = next(
        item
        for item in snapshot["routes"]
        if item["route"] == "/items/{item_id}" and item["method"] == "GET"
    )
    assert route["method"] == "GET"
    assert route["requests"] == 2
    assert route["status_classes"]["2xx"] == 2
    other = next(
        item
        for item in snapshot["routes"]
        if item["route"] == "/items/{item_id}" and item["method"] == "OTHER"
    )
    assert other["requests"] == 2
    snapshot_text = str(snapshot)
    assert "item-alpha-123" not in snapshot_text
    assert "item-beta-456" not in snapshot_text
    assert "query-secret" not in snapshot_text


def test_unhandled_exception_is_counted_as_server_error() -> None:
    app = _instrumented_app()

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("expected test failure")

    response = TestClient(app, raise_server_exceptions=False).get(
        "/boom",
        headers={"X-Request-ID": "failed-request"},
    )

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["X-Request-ID"] == "failed-request"
    route = next(item for item in http_metrics_snapshot()["routes"] if item["route"] == "/boom")
    assert route["requests"] == 1
    assert route["errors"] == 1
    assert route["status_classes"]["5xx"] == 1


def test_observability_endpoint_requires_ui_session_and_counts_auth_rejections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import auth as auth_api
    from app.main import app
    from app.services import api_gateway
    from app.services import auth as auth_service

    configured = True
    monkeypatch.setattr(auth_service, "is_configured", lambda: configured)
    monkeypatch.setattr(auth_service, "is_valid_session", lambda token: token == "valid-session")
    monkeypatch.setattr(auth_api, "_is_local_network", lambda _host: False)

    client = TestClient(app)
    unauthorized = client.get(
        "/api/observability",
        headers={"X-Request-ID": "auth-unauthorized"},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.headers["X-Request-ID"] == "auth-unauthorized"

    configured = False
    forbidden = client.get(
        "/api/observability",
        headers={"X-Request-ID": "auth-forbidden"},
    )
    assert forbidden.status_code == 403
    assert forbidden.headers["X-Request-ID"] == "auth-forbidden"

    configured = True
    client.cookies.set(auth_api.COOKIE_NAME, "valid-session")
    allowed = client.get("/api/observability")
    assert allowed.status_code == 200
    assert allowed.json()["process"]["started_at"].endswith("Z")

    assert api_gateway.required_scope("GET", "/api/observability") is None
    route = next(
        item
        for item in http_metrics_snapshot()["routes"]
        if item["route"] == "/api/observability"
    )
    assert route["status_classes"]["4xx"] == 2


def test_reset_clears_counters() -> None:
    app = _instrumented_app()

    @app.get("/ok")
    def ok() -> dict[str, bool]:
        return {"ok": True}

    assert TestClient(app).get("/ok").status_code == 200
    assert http_metrics_snapshot()["totals"]["requests"] == 1

    reset_http_metrics()

    snapshot = http_metrics_snapshot()
    assert snapshot["in_flight"] == 0
    assert snapshot["totals"]["requests"] == 0
    assert snapshot["routes"] == []
