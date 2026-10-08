from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings


def test_settings_reads_server_and_auth_values_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for name in (
        "HOST",
        "PORT",
        "LOG_LEVEL",
        "AUTH_EMAIL",
        "AUTH_PASSWORD",
        "AUTH_SMTP_HOST",
        "AUTH_SMTP_PORT",
        "AUTH_SMTP_SECURITY",
        "AUTH_SMTP_USERNAME",
        "AUTH_SMTP_PASSWORD",
        "AUTH_SMTP_FROM_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "HOST=127.0.0.1\n"
        "PORT=4318\n"
        "LOG_LEVEL=DEBUG\n"
        "AUTH_EMAIL=admin@example.com\n"
        "AUTH_PASSWORD=config-secret\n"
        "AUTH_SMTP_HOST=smtp.example.com\n"
        "AUTH_SMTP_PORT=587\n"
        "AUTH_SMTP_SECURITY=starttls\n"
        "AUTH_SMTP_USERNAME=sender@example.com\n"
        "AUTH_SMTP_PASSWORD=smtp-secret\n"
        "AUTH_SMTP_FROM_ADDRESS=no-reply@example.com\n",
        encoding="utf-8",
    )

    configured = Settings(_env_file=env_path)

    assert configured.host == "127.0.0.1"
    assert configured.port == 4318
    assert configured.log_level == "DEBUG"
    assert configured.auth_email == "admin@example.com"
    assert configured.auth_password == "config-secret"
    assert configured.auth_smtp_host == "smtp.example.com"
    assert configured.auth_smtp_port == 587
    assert configured.auth_smtp_security == "starttls"
    assert configured.auth_smtp_username == "sender@example.com"
    assert configured.auth_smtp_password == "smtp-secret"
    assert configured.auth_smtp_from_address == "no-reply@example.com"
