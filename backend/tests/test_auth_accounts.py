from __future__ import annotations

import stat
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as app_config
from app.api import auth as auth_api
from app.services import auth_verification, email_adapter


@pytest.fixture(autouse=True)
def isolated_auth_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[ModuleType]:
    monkeypatch.setattr(app_config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(app_config.settings, "auth_email", "")
    monkeypatch.setattr(app_config.settings, "auth_password", "")
    monkeypatch.setattr(app_config.settings, "auth_registration_secret", "")
    monkeypatch.setattr(app_config.settings, "auth_smtp_host", "")
    monkeypatch.setattr(app_config.settings, "auth_smtp_port", 465)
    monkeypatch.setattr(app_config.settings, "auth_smtp_security", "ssl")
    monkeypatch.setattr(app_config.settings, "auth_smtp_username", "")
    monkeypatch.setattr(app_config.settings, "auth_smtp_password", "")
    monkeypatch.setattr(app_config.settings, "auth_smtp_from_address", "")
    monkeypatch.setattr(app_config, "_ENV_FILE", tmp_path / ".env")

    from app.services import auth

    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()
    auth_api._registration_attempts.clear()
    yield auth
    auth._sessions.clear()
    auth._configured_cache = None
    auth_api._fail_counter.clear()
    auth_api._registration_attempts.clear()


def test_register_normalizes_email_and_rejects_duplicate(
    isolated_auth_store: ModuleType,
) -> None:
    auth = isolated_auth_store

    token, user = auth.register_user("  User@Example.COM ", "password-123")

    assert user["email"] == "user@example.com"
    assert auth.current_user(token) == user
    with pytest.raises(auth.EmailAlreadyRegisteredError):
        auth.register_user("user@example.com", "another-password")


def test_login_and_password_change_revoke_all_user_sessions(
    isolated_auth_store: ModuleType,
) -> None:
    auth = isolated_auth_store
    first_token, _ = auth.register_user("user@example.com", "password-123")
    second = auth.authenticate_user("USER@example.com", "password-123")

    assert second is not None
    second_token, _ = second
    assert auth.authenticate_user("user@example.com", "wrong-password") is None
    assert auth.change_password(first_token, "wrong-password", "new-password-456") is False
    assert auth.change_password(first_token, "password-123", "new-password-456") is True
    assert auth.is_valid_session(first_token) is False
    assert auth.is_valid_session(second_token) is False
    assert auth.authenticate_user("user@example.com", "password-123") is None
    assert auth.authenticate_user("user@example.com", "new-password-456") is not None


def test_legacy_password_must_be_bound_before_registration(
    isolated_auth_store: ModuleType,
) -> None:
    auth = isolated_auth_store
    auth.set_password("secret")

    assert auth.requires_legacy_migration() is True
    with pytest.raises(auth.LegacyMigrationRequiredError):
        auth.register_user("owner@example.com", "password-123")
    assert auth.migrate_legacy_user("owner@example.com", "wrong-secret") is None

    migrated = auth.migrate_legacy_user("Owner@Example.com", "secret")

    assert migrated is not None
    token, user = migrated
    assert user["email"] == "owner@example.com"
    assert auth.current_user(token) == user
    assert auth.requires_legacy_migration() is False
    assert auth.verify_and_create_session("secret") is None
    assert auth.authenticate_user("owner@example.com", "secret") is not None


def test_bootstrap_creates_email_account(
    isolated_auth_store: ModuleType,
) -> None:
    auth = isolated_auth_store
    app_config.settings.auth_email = "admin@example.com"
    app_config.settings.auth_password = "bootstrap-secret"

    assert auth.bootstrap_from_env() is True
    assert auth.has_users() is True
    assert auth.authenticate_user("admin@example.com", "bootstrap-secret") is not None
    assert auth.bootstrap_from_env() is False


def test_registration_secret_is_hashed_and_preserved(
    isolated_auth_store: ModuleType,
) -> None:
    auth = isolated_auth_store
    secret = "test-registration-secret"

    auth.set_registration_secret(secret)
    auth_path = app_config.settings.data_dir / "user_data" / "auth.json"

    assert auth.has_registration_secret() is True
    assert auth.verify_registration_secret(secret) is True
    assert auth.verify_registration_secret("wrong-secret") is False
    assert secret not in auth_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600

    auth.register_user("user@example.com", "password-123")

    assert auth.verify_registration_secret(secret) is True


def test_registration_secret_bootstraps_once_from_env(
    isolated_auth_store: ModuleType,
) -> None:
    auth = isolated_auth_store
    env_path = app_config.settings.data_dir / ".env"
    app_config._ENV_FILE = env_path
    env_path.write_text(
        "AUTH_REGISTRATION_SECRET='first-secret'\n",
        encoding="utf-8",
    )

    assert auth.bootstrap_registration_secret_from_env() is True
    assert auth.verify_registration_secret("first-secret") is True

    env_path.write_text(
        "AUTH_REGISTRATION_SECRET='replacement-secret'\n",
        encoding="utf-8",
    )

    assert auth.bootstrap_registration_secret_from_env() is False
    assert auth.verify_registration_secret("first-secret") is True
    assert auth.verify_registration_secret("replacement-secret") is False


def test_account_api_register_login_change_password(
    isolated_auth_store: ModuleType,
) -> None:
    app = FastAPI()
    app.include_router(auth_api.router)
    client = TestClient(app)

    missing_code = client.post(
        "/api/auth/register",
        json={
            "email": "User@example.com",
            "password": "password-123",
            "code": "123456",
        },
    )
    assert missing_code.status_code == 400

    code = auth_verification.issue_code("User@example.com")
    registered = client.post(
        "/api/auth/register",
        json={
            "email": "User@example.com",
            "password": "password-123",
            "code": code,
        },
    )
    assert registered.status_code == 201
    assert registered.json()["user"]["email"] == "user@example.com"
    assert client.get("/api/auth/status").json()["authenticated"] is True

    wrong_old_password = client.post(
        "/api/auth/change-password",
        json={"old_password": "wrong-password", "new_password": "new-password-456"},
    )
    assert wrong_old_password.status_code == 400
    assert client.get("/api/auth/status").json()["authenticated"] is True

    changed = client.post(
        "/api/auth/change-password",
        json={"old_password": "password-123", "new_password": "new-password-456"},
    )
    assert changed.status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is False

    failed = client.post(
        "/api/auth/login",
        json={"email": "user@example.com", "password": "password-123"},
    )
    assert failed.status_code == 401
    logged_in = client.post(
        "/api/auth/login",
        json={"email": "USER@example.com", "password": "new-password-456"},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["user"]["email"] == "user@example.com"


def test_registration_code_endpoint_sends_and_enforces_cooldown(
    isolated_auth_store: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_auth_store.set_registration_secret("test-registration-secret")
    app_config.settings.auth_smtp_host = "smtp.example.com"
    app_config.settings.auth_smtp_username = "sender@example.com"
    app_config.settings.auth_smtp_password = "smtp-secret"
    monkeypatch.setattr(auth_verification, "_generate_code", lambda: "123456")
    deliveries: list[tuple[dict, str, str, str]] = []

    def fake_send(
        config: dict,
        password: str,
        subject: str,
        body: str,
        *,
        max_attempts: int,
    ) -> bool:
        assert max_attempts == 1
        deliveries.append((config, password, subject, body))
        return True

    monkeypatch.setattr(email_adapter, "send_email", fake_send)
    app = FastAPI()
    app.include_router(auth_api.router)
    client = TestClient(app)

    rejected = client.post(
        "/api/auth/register/code",
        json={
            "email": "New.User@example.com",
            "registration_secret": "wrong-secret",
        },
    )
    assert rejected.status_code == 403
    assert deliveries == []

    sent = client.post(
        "/api/auth/register/code",
        json={
            "email": "New.User@example.com",
            "registration_secret": "test-registration-secret",
        },
    )

    assert sent.status_code == 200
    assert sent.json() == {"ok": True, "cooldown_seconds": 60}
    config, password, subject, body = deliveries[0]
    assert config["to_addresses"] == ["new.user@example.com"]
    assert password == "smtp-secret"
    assert "注册验证码" in subject
    assert "123456" in body

    cooldown = client.post(
        "/api/auth/register/code",
        json={
            "email": "new.user@example.com",
            "registration_secret": "test-registration-secret",
        },
    )
    assert cooldown.status_code == 429
    assert int(cooldown.headers["retry-after"]) > 0

    registered = client.post(
        "/api/auth/register",
        json={
            "email": "new.user@example.com",
            "password": "password-123",
            "code": "123456",
        },
    )
    assert registered.status_code == 201


def test_verification_code_is_hashed_and_consumed_once(
    isolated_auth_store: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_verification, "_generate_code", lambda: "654321")

    code = auth_verification.issue_code("user@example.com")
    verification_path = (
        app_config.settings.data_dir / "user_data" / "auth_verifications.json"
    )

    assert code == "654321"
    assert "654321" not in verification_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(verification_path.stat().st_mode) == 0o600
    auth_verification.consume_code("user@example.com", code)
    with pytest.raises(
        auth_verification.VerificationCodeInvalidError,
        match="请先发送验证码",
    ):
        auth_verification.consume_code("user@example.com", code)


def test_verification_code_expires_after_ten_minutes(
    isolated_auth_store: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(auth_verification.time, "time", lambda: now)
    code = auth_verification.issue_code("user@example.com")
    now += auth_verification.CODE_TTL_SECONDS + 1

    with pytest.raises(
        auth_verification.VerificationCodeExpiredError,
        match="验证码已过期",
    ):
        auth_verification.consume_code("user@example.com", code)


def test_verification_code_is_revoked_after_five_wrong_attempts(
    isolated_auth_store: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth_verification, "_generate_code", lambda: "123456")
    code = auth_verification.issue_code("user@example.com")

    for attempts_left in range(4, 0, -1):
        with pytest.raises(
            auth_verification.VerificationCodeInvalidError,
            match=rf"还可尝试 {attempts_left} 次",
        ):
            auth_verification.consume_code("user@example.com", "000000")
    with pytest.raises(
        auth_verification.VerificationCodeInvalidError,
        match="错误次数过多",
    ):
        auth_verification.consume_code("user@example.com", "000000")
    with pytest.raises(
        auth_verification.VerificationCodeInvalidError,
        match="请先发送验证码",
    ):
        auth_verification.consume_code("user@example.com", code)


def test_failed_registration_email_discards_the_code(
    isolated_auth_store: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_config.settings.auth_smtp_host = "smtp.example.com"
    app_config.settings.auth_smtp_username = "sender@example.com"
    monkeypatch.setattr(auth_verification, "_generate_code", lambda: "123456")
    monkeypatch.setattr(email_adapter, "send_email", lambda *args, **kwargs: False)

    with pytest.raises(
        auth_verification.VerificationEmailUnavailableError,
        match="邮件发送失败",
    ):
        auth_verification.send_registration_code("user@example.com")
    with pytest.raises(
        auth_verification.VerificationCodeInvalidError,
        match="请先发送验证码",
    ):
        auth_verification.consume_code("user@example.com", "123456")
