"""Email verification codes used by public account registration."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path

from app.services import auth, email_adapter
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

CODE_TTL_SECONDS = 10 * 60
RESEND_COOLDOWN_SECONDS = 60
MAX_VERIFY_ATTEMPTS = 5

_SCHEMA_VERSION = 1
_PBKDF2_ITERATIONS = 120_000
_SALT_BYTES = 16
_lock = threading.Lock()


class VerificationCodeError(ValueError):
    """Base class for registration-code errors."""


class VerificationCodeCooldownError(VerificationCodeError):
    """Raised when a new code is requested during the resend cooldown."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(f"请 {self.retry_after} 秒后重新发送")


class VerificationCodeInvalidError(VerificationCodeError):
    """Raised when a code is missing, invalid, or exhausted."""


class VerificationCodeExpiredError(VerificationCodeError):
    """Raised when a code has expired."""


class VerificationEmailUnavailableError(RuntimeError):
    """Raised when registration email cannot be delivered."""


def _path() -> Path:
    from app.config import settings

    path = settings.data_dir / "user_data" / "auth_verifications.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load() -> dict:
    path = _path()
    if not path.exists():
        return {"schema_version": _SCHEMA_VERSION, "codes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("auth_verifications.json malformed: %s", exc)
        return {"schema_version": _SCHEMA_VERSION, "codes": {}}
    if not isinstance(data, dict) or not isinstance(data.get("codes"), dict):
        return {"schema_version": _SCHEMA_VERSION, "codes": {}}
    return data


def _save(data: dict) -> None:
    atomic_write_text(
        _path(),
        json.dumps(data, indent=2, ensure_ascii=False),
        mode=0o600,
    )


def _hash_code(code: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        code.encode("ascii"),
        salt,
        _PBKDF2_ITERATIONS,
    ).hex()


def _generate_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _discard_code(email: str, expected_hash: str) -> None:
    with _lock:
        data = _load()
        record = data["codes"].get(email)
        if isinstance(record, dict) and record.get("code_hash") == expected_hash:
            data["codes"].pop(email, None)
            _save(data)


def issue_code(email: str) -> str:
    """Create and persist a code, returning the plaintext for immediate delivery."""
    normalized = auth.normalize_email(email)
    now = time.time()
    with _lock:
        data = _load()
        for saved_email, saved_record in list(data["codes"].items()):
            expires_at = (
                saved_record.get("expires_at", 0)
                if isinstance(saved_record, dict)
                else 0
            )
            if not isinstance(expires_at, (int, float)) or expires_at <= now:
                data["codes"].pop(saved_email, None)
        record = data["codes"].get(normalized)
        resend_at = record.get("resend_at", 0) if isinstance(record, dict) else 0
        if isinstance(resend_at, (int, float)) and resend_at > now:
            raise VerificationCodeCooldownError(int(resend_at - now + 0.999))

        code = _generate_code()
        salt = os.urandom(_SALT_BYTES)
        data["schema_version"] = _SCHEMA_VERSION
        data["codes"][normalized] = {
            "code_hash": _hash_code(code, salt),
            "salt": salt.hex(),
            "expires_at": now + CODE_TTL_SECONDS,
            "resend_at": now + RESEND_COOLDOWN_SECONDS,
            "attempts_left": MAX_VERIFY_ATTEMPTS,
        }
        _save(data)
    return code


def _registration_smtp(email: str) -> tuple[dict, str]:
    from app.config import settings

    if settings.auth_smtp_host.strip():
        return {
            "host": settings.auth_smtp_host.strip(),
            "port": settings.auth_smtp_port,
            "security": settings.auth_smtp_security.strip().lower(),
            "username": settings.auth_smtp_username.strip(),
            "from_address": (
                settings.auth_smtp_from_address.strip()
                or settings.auth_smtp_username.strip()
            ),
            "to_addresses": [email],
        }, settings.auth_smtp_password

    from app import secrets_store
    from app.services import preferences

    config = {**preferences.get_email_smtp_config(), "to_addresses": [email]}
    return config, secrets_store.get_email_smtp_password()


def send_registration_code(email: str) -> int:
    """Issue and send a registration code, returning the resend cooldown."""
    normalized = auth.normalize_email(email)
    config, password = _registration_smtp(normalized)
    if not email_adapter.is_configured(config):
        raise VerificationEmailUnavailableError("注册邮件服务尚未配置")

    code = issue_code(normalized)
    record = _load()["codes"].get(normalized, {})
    code_hash = str(record.get("code_hash") or "")
    sent = email_adapter.send_email(
        config,
        password,
        "Tick Stock Panel 注册验证码",
        (
            f"你的注册验证码是: {code}\n\n"
            f"验证码将在 {CODE_TTL_SECONDS // 60} 分钟后失效, 请勿转发给他人。\n"
            "如果不是你本人操作, 请忽略此邮件。"
        ),
        max_attempts=1,
    )
    if not sent:
        _discard_code(normalized, code_hash)
        raise VerificationEmailUnavailableError("验证码邮件发送失败, 请稍后重试")
    return RESEND_COOLDOWN_SECONDS


def consume_code(email: str, code: str) -> None:
    """Validate and consume a code. Invalid attempts are persisted."""
    normalized = auth.normalize_email(email)
    now = time.time()
    with _lock:
        data = _load()
        record = data["codes"].get(normalized)
        if not isinstance(record, dict):
            raise VerificationCodeInvalidError("请先发送验证码")

        expires_at = record.get("expires_at", 0)
        if not isinstance(expires_at, (int, float)) or expires_at <= now:
            data["codes"].pop(normalized, None)
            _save(data)
            raise VerificationCodeExpiredError("验证码已过期, 请重新发送")

        try:
            salt = bytes.fromhex(str(record.get("salt") or ""))
            expected = bytes.fromhex(str(record.get("code_hash") or ""))
            actual = bytes.fromhex(_hash_code(str(code), salt))
        except (TypeError, ValueError):
            expected = b""
            actual = b"\0"

        if not secrets.compare_digest(actual, expected):
            try:
                attempts_left = max(0, int(record.get("attempts_left", 0)) - 1)
            except (TypeError, ValueError):
                attempts_left = 0
            if attempts_left == 0:
                data["codes"].pop(normalized, None)
                message = "验证码错误次数过多, 请重新发送"
            else:
                record["attempts_left"] = attempts_left
                message = f"验证码错误, 还可尝试 {attempts_left} 次"
            _save(data)
            raise VerificationCodeInvalidError(message)

        data["codes"].pop(normalized, None)
        _save(data)
