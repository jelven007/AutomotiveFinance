"""Authentication must be server-only and absent from frozen desktop builds."""
from __future__ import annotations

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from app import config as app_config
from app.api import auth as auth_api
from app.main import auth_middleware


def _request(path: str, method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 3018),
        }
    )


@pytest.mark.asyncio
async def test_frozen_desktop_bypasses_business_api_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_config, "_IS_FROZEN", True)
    called = False

    async def call_next(_request: Request) -> JSONResponse:
        nonlocal called
        called = True
        return JSONResponse({"ok": True})

    response = await auth_middleware(_request("/api/capabilities"), call_next)

    assert response.status_code == 200
    assert called is True


@pytest.mark.asyncio
async def test_frozen_desktop_disables_authentication_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_config, "_IS_FROZEN", True)

    async def call_next(_request: Request) -> JSONResponse:
        raise AssertionError("desktop authentication request reached the router")

    response = await auth_middleware(
        _request("/api/auth/login", method="POST"),
        call_next,
    )

    assert response.status_code == 404
    assert b"AUTH_DISABLED" in response.body


def test_frozen_desktop_status_declares_authentication_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_config, "_IS_FROZEN", True)

    status = auth_api.auth_status(_request("/api/auth/status"))

    assert status == {
        "auth_required": False,
        "configured": False,
        "has_users": False,
        "legacy_migration_required": False,
        "registration_enabled": False,
        "email_verification_required": False,
        "authenticated": True,
        "user": None,
    }
