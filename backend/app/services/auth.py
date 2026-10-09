"""Email account authentication for the self-hosted web UI.

Accounts only control access to the shared application data. This first phase
does not introduce tenants, roles, or password recovery. Public registration
requires a short-lived email verification code.
Legacy password-only installations remain readable until the owner binds an
email address with the existing password.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets as _secrets
import threading
import time
import uuid
from pathlib import Path

from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 3
_PBKDF2_ITER = 200_000
_SALT_LEN = 16
_TOKEN_BYTES = 32
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DUMMY_SALT = b"\0" * _SALT_LEN
_DUMMY_HASH = hashlib.pbkdf2_hmac(
    "sha256", b"invalid-account", _DUMMY_SALT, _PBKDF2_ITER,
).hex()
# Packaged desktop builds cannot depend on a deployment .env. These constants
# contain only the one-way PBKDF2 representation of the default registration
# secret; the plaintext is never shipped or written to auth.json.
_DESKTOP_REGISTRATION_SECRET_SALT = "d7b9ccb0b1499629460f9f58a9661de7"
_DESKTOP_REGISTRATION_SECRET_HASH = (
    "db25f63bb9ebde3c5260c28b023a6290857e486273a3226836134c3e6dd8bd77"
)

SESSION_TTL = 30 * 24 * 3600

_lock = threading.Lock()
# token -> (expires_at, user_id). user_id=None denotes a legacy password session.
_sessions: dict[str, tuple[float, str | None]] = {}
_configured_cache: bool | None = None


class EmailAlreadyRegisteredError(ValueError):
    """Raised when an email address already owns an account."""


class LegacyMigrationRequiredError(ValueError):
    """Raised when password-only auth must be claimed before registration."""


def _path() -> Path:
    from app.config import settings

    path = settings.data_dir / "user_data" / "auth.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load() -> dict:
    path = _path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("auth.json malformed: %s", exc)
    return {}


def _save(data: dict) -> None:
    atomic_write_text(
        _path(),
        json.dumps(data, indent=2, ensure_ascii=False),
        mode=0o600,
    )


def normalize_email(email: str) -> str:
    normalized = str(email or "").strip().casefold()
    if (
        len(normalized) > 254
        or not _EMAIL_RE.fullmatch(normalized)
        or normalized.startswith(".")
        or ".." in normalized
    ):
        raise ValueError("请输入有效的邮箱地址")
    return normalized


def _validate_password(password: str, *, minimum: int = 8) -> None:
    if len(password) < minimum:
        raise ValueError(f"密码至少 {minimum} 位")
    if len(password) > 128:
        raise ValueError("密码不能超过 128 位")


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = os.urandom(_SALT_LEN)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITER,
    )
    return salt.hex(), digest.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITER,
    )
    return _secrets.compare_digest(actual, expected)


def _registration_secret_fields(data: dict) -> dict:
    return {
        key: data[key]
        for key in ("registration_secret_hash", "registration_secret_salt")
        if data.get(key)
    }


def has_registration_secret() -> bool:
    data = _load()
    return bool(
        data.get("registration_secret_hash")
        and data.get("registration_secret_salt")
    )


def set_registration_secret(secret: str) -> None:
    """Hash and persist the shared registration secret."""
    value = str(secret or "").strip()
    if not 1 <= len(value) <= 128:
        raise ValueError("注册口令长度必须在 1 到 128 位之间")
    salt_hex, hash_hex = _hash_password(value)
    with _lock:
        data = _load()
        data["schema_version"] = _SCHEMA_VERSION
        data["registration_secret_hash"] = hash_hex
        data["registration_secret_salt"] = salt_hex
        data["updated_at"] = int(time.time())
        data.setdefault("sessions", {})
        _save(data)
    logger.info("registration secret hash initialized")


def verify_registration_secret(secret: str) -> bool:
    """Verify without exposing whether malformed hash data was persisted."""
    value = str(secret or "")
    data = _load()
    salt_hex = str(data.get("registration_secret_salt") or _DUMMY_SALT.hex())
    hash_hex = str(data.get("registration_secret_hash") or _DUMMY_HASH)
    return has_registration_secret() and _verify_password(value, salt_hex, hash_hex)


def _users(data: dict) -> dict[str, dict]:
    users = data.get("users")
    return users if isinstance(users, dict) else {}


def _find_user_by_email(data: dict, email: str) -> dict | None:
    for user in _users(data).values():
        if isinstance(user, dict) and user.get("email") == email:
            return user
    return None


def _public_user(user: dict) -> dict:
    return {
        "id": str(user["id"]),
        "email": str(user["email"]),
        "created_at": int(user["created_at"]),
    }


def _new_user(email: str, password: str) -> dict:
    _validate_password(password)
    salt_hex, hash_hex = _hash_password(password)
    now = int(time.time())
    return {
        "id": uuid.uuid4().hex,
        "email": email,
        "password_hash": hash_hex,
        "password_salt": salt_hex,
        "created_at": now,
        "updated_at": now,
    }


def _new_session_locked(data: dict, user_id: str | None) -> str:
    token = _secrets.token_urlsafe(_TOKEN_BYTES)
    expires_at = time.time() + SESSION_TTL
    _sessions[token] = (expires_at, user_id)
    saved = data.setdefault("sessions", {})
    saved[token] = {"expires_at": expires_at, "user_id": user_id}
    return token


def has_users() -> bool:
    return bool(_users(_load()))


def is_email_registered(email: str) -> bool:
    normalized = normalize_email(email)
    return _find_user_by_email(_load(), normalized) is not None


def requires_legacy_migration() -> bool:
    data = _load()
    return bool(data.get("password_hash")) and not bool(_users(data))


def is_configured() -> bool:
    """Whether any account or a legacy access password protects the app."""
    global _configured_cache
    if _configured_cache is None:
        data = _load()
        _configured_cache = bool(_users(data) or data.get("password_hash"))
    return _configured_cache


def register_user(email: str, password: str) -> tuple[str, dict]:
    """Create an email account and an authenticated session."""
    global _configured_cache
    normalized = normalize_email(email)
    _validate_password(password)
    with _lock:
        data = _load()
        if data.get("password_hash") and not _users(data):
            raise LegacyMigrationRequiredError(
                "现有访问密码尚未绑定邮箱, 请先升级原账户",
            )
        if _find_user_by_email(data, normalized):
            raise EmailAlreadyRegisteredError("该邮箱已注册")
        user = _new_user(normalized, password)
        data = {
            "schema_version": _SCHEMA_VERSION,
            **_registration_secret_fields(data),
            "users": {**_users(data), user["id"]: user},
            "sessions": data.get("sessions") or {},
            "updated_at": int(time.time()),
        }
        token = _new_session_locked(data, user["id"])
        _save(data)
    _configured_cache = True
    logger.info("auth account registered")
    return token, _public_user(user)


def migrate_legacy_user(email: str, password: str) -> tuple[str, dict] | None:
    """Bind an email to the previous password-only account."""
    global _configured_cache
    normalized = normalize_email(email)
    _validate_password(password, minimum=6)
    with _lock:
        data = _load()
        if _users(data) or not data.get("password_hash"):
            return None
        if not _verify_password(
            password,
            data.get("password_salt", ""),
            data["password_hash"],
        ):
            return None
        # Existing passwords may be six or seven characters. Migration keeps
        # the exact password; the eight-character rule applies on next change.
        salt_hex, hash_hex = _hash_password(password)
        now = int(time.time())
        user = {
            "id": uuid.uuid4().hex,
            "email": normalized,
            "password_hash": hash_hex,
            "password_salt": salt_hex,
            "created_at": now,
            "updated_at": now,
        }
        _sessions.clear()
        migrated = {
            "schema_version": _SCHEMA_VERSION,
            **_registration_secret_fields(data),
            "users": {user["id"]: user},
            "sessions": {},
            "updated_at": now,
        }
        token = _new_session_locked(migrated, user["id"])
        _save(migrated)
    _configured_cache = True
    logger.info("legacy auth migrated to email account")
    return token, _public_user(user)


def authenticate_user(email: str, password: str) -> tuple[str, dict] | None:
    try:
        normalized = normalize_email(email)
    except ValueError:
        normalized = ""
    with _lock:
        data = _load()
        user = _find_user_by_email(data, normalized) if normalized else None
        if user is None:
            _verify_password(password, _DUMMY_SALT.hex(), _DUMMY_HASH)
            return None
        if not _verify_password(
            password,
            user.get("password_salt", ""),
            user.get("password_hash", ""),
        ):
            return None
        token = _new_session_locked(data, str(user["id"]))
        _save(data)
    return token, _public_user(user)


def current_user(token: str) -> dict | None:
    if not is_valid_session(token):
        return None
    with _lock:
        session = _sessions.get(token)
        if not session or session[1] is None:
            return None
        user = _users(_load()).get(session[1])
        return _public_user(user) if isinstance(user, dict) else None


def change_password(token: str, old_password: str, new_password: str) -> bool:
    """Change the current account password and revoke all its sessions."""
    _validate_password(new_password)
    if not is_valid_session(token):
        return False
    with _lock:
        session = _sessions.get(token)
        user_id = session[1] if session else None
        data = _load()
        user = _users(data).get(user_id) if user_id else None
        if not isinstance(user, dict) or not _verify_password(
            old_password,
            user.get("password_salt", ""),
            user.get("password_hash", ""),
        ):
            return False
        salt_hex, hash_hex = _hash_password(new_password)
        user["password_salt"] = salt_hex
        user["password_hash"] = hash_hex
        user["updated_at"] = int(time.time())
        for saved_token, (_, saved_user_id) in list(_sessions.items()):
            if saved_user_id == user_id:
                _sessions.pop(saved_token, None)
        data["sessions"] = {
            saved_token: {"expires_at": expires_at, "user_id": saved_user_id}
            for saved_token, (expires_at, saved_user_id) in _sessions.items()
        }
        data["updated_at"] = int(time.time())
        _save(data)
    return True


def set_password(password: str) -> None:
    """Compatibility initializer for historical password-only deployments."""
    global _configured_cache
    _validate_password(password, minimum=6)
    salt_hex, hash_hex = _hash_password(password)
    with _lock:
        data = _load()
        _sessions.clear()
        _save({
            "schema_version": _SCHEMA_VERSION,
            **_registration_secret_fields(data),
            "password_hash": hash_hex,
            "password_salt": salt_hex,
            "updated_at": int(time.time()),
            "sessions": {},
        })
    _configured_cache = True
    logger.info("legacy access password set")


def bootstrap_registration_secret_from_env() -> bool:
    """Persist a one-way hash from AUTH_REGISTRATION_SECRET once."""
    from app.config import _ENV_FILE, settings

    secret = (settings.auth_registration_secret or "").strip()
    if _ENV_FILE.is_file():
        from dotenv import dotenv_values

        raw = dotenv_values(_ENV_FILE, encoding="utf-8", interpolate=False)
        raw_secret = raw.get("AUTH_REGISTRATION_SECRET")
        if isinstance(raw_secret, str) and raw_secret.strip():
            secret = raw_secret.strip()
    if not secret or has_registration_secret():
        return False
    try:
        set_registration_secret(secret)
        return True
    except ValueError as exc:
        logger.warning("registration secret bootstrap skipped: %s", exc)
        return False


def bootstrap_registration_secret_for_desktop() -> bool:
    """Initialize the packaged desktop default without storing plaintext."""
    from app.config import _IS_FROZEN

    if not _IS_FROZEN:
        return False
    with _lock:
        data = _load()
        if (
            data.get("registration_secret_hash")
            and data.get("registration_secret_salt")
        ):
            return False
        data["schema_version"] = _SCHEMA_VERSION
        data["registration_secret_hash"] = _DESKTOP_REGISTRATION_SECRET_HASH
        data["registration_secret_salt"] = _DESKTOP_REGISTRATION_SECRET_SALT
        data["updated_at"] = int(time.time())
        data.setdefault("sessions", {})
        _save(data)
    logger.info("desktop registration secret hash initialized")
    return True


def bootstrap_from_env() -> bool:
    """Initialize auth once from AUTH_EMAIL/AUTH_PASSWORD when provided."""
    from app.config import _ENV_FILE, settings

    password = (settings.auth_password or "").strip()
    email = (settings.auth_email or "").strip()
    if _ENV_FILE.is_file():
        from dotenv import dotenv_values

        raw = dotenv_values(_ENV_FILE, encoding="utf-8", interpolate=False)
        raw_password = raw.get("AUTH_PASSWORD")
        raw_email = raw.get("AUTH_EMAIL")
        if isinstance(raw_password, str) and raw_password.strip():
            password = raw_password.strip()
        if isinstance(raw_email, str) and raw_email.strip():
            email = raw_email.strip()
    if not password or is_configured():
        return False
    try:
        if email:
            token, _ = register_user(email, password)
            revoke_session(token)
            logger.info("auth account bootstrapped from environment")
        else:
            set_password(password)
            logger.info("legacy auth bootstrapped from AUTH_PASSWORD")
        return True
    except ValueError as exc:
        logger.warning("auth environment bootstrap skipped: %s", exc)
        return False


def verify_and_create_session(password: str) -> str | None:
    """Compatibility login for a password-only auth.json."""
    with _lock:
        data = _load()
        if _users(data) or not data.get("password_hash"):
            return None
        if not _verify_password(
            password,
            data.get("password_salt", ""),
            data["password_hash"],
        ):
            return None
        token = _new_session_locked(data, None)
        _save(data)
        return token


def revoke_session(token: str) -> None:
    with _lock:
        _sessions.pop(token, None)
        _persist_sessions_locked()


def is_valid_session(token: str) -> bool:
    if not token:
        return False
    with _lock:
        session = _sessions.get(token)
        if session is None:
            return False
        if time.time() > session[0]:
            _sessions.pop(token, None)
            _persist_sessions_locked()
            return False
        return True


def _persist_sessions_locked() -> None:
    data = _load()
    data["sessions"] = {
        token: {"expires_at": expires_at, "user_id": user_id}
        for token, (expires_at, user_id) in _sessions.items()
    }
    _save(data)


def _restore_sessions() -> None:
    with _lock:
        data = _load()
        now = time.time()
        saved = data.get("sessions") or {}
        changed = False
        for token, record in saved.items():
            if isinstance(record, (int, float)):
                expires_at, user_id = float(record), None
            elif isinstance(record, dict):
                expires_at = record.get("expires_at")
                user_id = record.get("user_id")
            else:
                changed = True
                continue
            if isinstance(expires_at, (int, float)) and expires_at > now:
                _sessions[token] = (float(expires_at), str(user_id) if user_id else None)
            else:
                changed = True
        if changed:
            _persist_sessions_locked()


try:
    _restore_sessions()
except Exception as exc:
    logger.warning("restore sessions failed: %s", exc)
