"""Email account and legacy password authentication API."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.services import auth, auth_verification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "tf_session"
_COOKIE_MAX_AGE = 30 * 24 * 3600  # 与 SESSION_TTL 一致

# 限流: { ip: (fail_count, lock_until_ts) }
_fail_counter: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))
_fail_lock = Lock()
_MAX_FAILS = 5
_LOCK_SECONDS = 300

# Bound verification-email attempts per source to prevent a public instance
# from becoming an SMTP relay without adding a database or external limiter.
_registration_attempts: dict[str, list[float]] = defaultdict(list)
_MAX_REGISTRATIONS_PER_HOUR = 10


def _is_local_network(host: str | None) -> bool:
    """是否本机或内网请求。

    反向代理(Nginx)场景下 request.client.host 是代理本身(127.0.0.1),
    需信任 X-Forwarded-For 的最左(原始客户端)。本项目部署若经反代,
    请在反代配置正确的 X-Forwarded-For(标准做法)。
    """
    if not host:
        return False
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    # 内网网段: 10.x / 172.16-31.x / 192.168.x
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except (IndexError, ValueError):
            pass
    return False


def _client_ip(request: Request) -> str:
    """取真实客户端 IP。

    安全关键: 仅当直连 peer(request.client.host)本身是回环/内网地址
    (即请求确实经过同机/内网的可信反代)时, 才采信 X-Forwarded-For。
    否则公网请求可伪造 `X-Forwarded-For: 127.0.0.1` 冒充内网, 绕过
    「未设密码仅本机可访问」闸门、抢占 setup 端点、并绕过登录限流。
    """
    direct = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    if xff and direct and _is_local_network(direct):
        return xff.split(",")[0].strip()
    return direct or "unknown"


def _check_login_rate_limit(ip: str) -> None:
    """登录失败限流检查, 触发则抛 429。锁定过期后重置计数(重新给 5 次机会)。"""
    with _fail_lock:
        _count, until = _fail_counter.get(ip, (0, 0.0))
        now = time.time()
        if until > now:
            wait = int(until - now)
            raise HTTPException(
                status_code=429,
                detail=f"登录失败次数过多, 请 {wait} 秒后重试",
            )
        if until and until <= now:
            # 锁定已过期: 清除旧计数, 否则之后每失败一次都会立刻再锁 5 分钟
            _fail_counter.pop(ip, None)


def _record_login_fail(ip: str) -> None:
    """记录一次登录失败, 达阈值则锁定。"""
    with _fail_lock:
        # 防内存膨胀: 条目过多时清掉已过锁定期的记录
        if len(_fail_counter) > 1000:
            now = time.time()
            for stale in [k for k, (_, u) in _fail_counter.items() if u <= now]:
                _fail_counter.pop(stale, None)
        count, until = _fail_counter.get(ip, (0, 0.0))
        count += 1
        if count >= _MAX_FAILS:
            until = time.time() + _LOCK_SECONDS
            logger.warning("auth login locked for %s after %d fails", ip, count)
        _fail_counter[ip] = (count, until)


def _clear_login_fails(ip: str) -> None:
    """登录成功后清除该 IP 的失败计数。"""
    with _fail_lock:
        _fail_counter.pop(ip, None)


def _record_registration_attempt(ip: str) -> None:
    now = time.time()
    cutoff = now - 3600
    with _fail_lock:
        if len(_registration_attempts) > 1000:
            for stale_ip in [
                key
                for key, timestamps in _registration_attempts.items()
                if not timestamps or timestamps[-1] <= cutoff
            ]:
                _registration_attempts.pop(stale_ip, None)
        recent = [timestamp for timestamp in _registration_attempts[ip] if timestamp > cutoff]
        if len(recent) >= _MAX_REGISTRATIONS_PER_HOUR:
            raise HTTPException(
                status_code=429,
                detail="注册请求过于频繁, 请稍后重试",
            )
        recent.append(now)
        _registration_attempts[ip] = recent


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    direct = request.client.host if request.client else ""
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_https = request.url.scheme == "https" or (
        _is_local_network(direct) and forwarded_proto.split(",")[0].strip() == "https"
    )
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
        secure=is_https,
    )


# ================================================================
# 端点
# ================================================================

class PasswordIn(BaseModel):
    password: str = Field(min_length=6, max_length=128)


class LoginIn(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class AccountIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class RegistrationCodeIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    registration_secret: str = Field(min_length=1, max_length=128)


class LegacyMigrationIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=6, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


@router.get("/status")
def auth_status(request: Request) -> dict:
    """Return account availability and the current browser session."""
    token = request.cookies.get(COOKIE_NAME)
    authenticated = bool(token and auth.is_valid_session(token))
    return {
        "configured": auth.is_configured(),
        "has_users": auth.has_users(),
        "legacy_migration_required": auth.requires_legacy_migration(),
        "registration_enabled": auth.has_registration_secret(),
        "email_verification_required": True,
        "authenticated": authenticated,
        "user": auth.current_user(token) if authenticated and token else None,
    }


@router.post("/register/code")
def send_registration_code(req: RegistrationCodeIn, request: Request) -> dict:
    """Verify the registration secret, then email a short-lived code."""
    try:
        normalized = auth.normalize_email(req.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if auth.requires_legacy_migration():
        raise HTTPException(
            status_code=409,
            detail="现有访问密码尚未绑定邮箱, 请先升级原账户",
            headers={"X-Auth-Action": "migrate"},
        )

    _record_registration_attempt(_client_ip(request))
    if not auth.has_registration_secret():
        raise HTTPException(status_code=503, detail="注册口令尚未配置")
    if not auth.verify_registration_secret(req.registration_secret):
        raise HTTPException(status_code=403, detail="注册口令错误")
    if auth.is_email_registered(normalized):
        raise HTTPException(status_code=409, detail="该邮箱已注册")
    try:
        cooldown = auth_verification.send_registration_code(normalized)
    except auth_verification.VerificationCodeCooldownError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except auth_verification.VerificationEmailUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"ok": True, "cooldown_seconds": cooldown}


@router.post("/register", status_code=201)
def register(req: AccountIn, request: Request, response: Response) -> dict:
    """Create an email account and sign the browser in."""
    try:
        if auth.is_email_registered(req.email):
            raise auth.EmailAlreadyRegisteredError("该邮箱已注册")
        if auth.requires_legacy_migration():
            raise auth.LegacyMigrationRequiredError(
                "现有访问密码尚未绑定邮箱, 请先升级原账户",
            )
        auth_verification.consume_code(req.email, req.code)
        token, user = auth.register_user(req.email, req.password)
    except auth_verification.VerificationCodeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except auth.EmailAlreadyRegisteredError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except auth.LegacyMigrationRequiredError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Auth-Action": "migrate"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_session_cookie(response, request, token)
    logger.info("auth registration completed from %s", _client_ip(request))
    return {"ok": True, "authenticated": True, "user": user}


@router.post("/migrate")
def migrate_legacy_account(
    req: LegacyMigrationIn,
    request: Request,
    response: Response,
) -> dict:
    """Bind an email to a password-only installation without losing access."""
    ip = _client_ip(request)
    _check_login_rate_limit(ip)
    if not auth.requires_legacy_migration():
        raise HTTPException(status_code=409, detail="当前系统无需升级旧账户")
    try:
        result = auth.migrate_legacy_user(req.email, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result is None:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="原访问密码错误")
    token, user = result
    _clear_login_fails(ip)
    _set_session_cookie(response, request, token)
    return {"ok": True, "authenticated": True, "user": user}


@router.post("/setup")
def setup_password(req: PasswordIn, request: Request) -> dict:
    """Compatibility endpoint for creating a legacy access password."""
    client_ip = _client_ip(request)
    if not _is_local_network(client_ip):
        logger.warning("setup rejected from non-local ip: %s", client_ip)
        raise HTTPException(
            status_code=403,
            detail="首次设置密码仅允许本机或内网访问,请通过 SSH/本地浏览器操作",
        )

    if auth.is_configured():
        raise HTTPException(status_code=409, detail="密码已设置,如需修改请登录后使用改密码功能")

    auth.set_password(req.password)
    logger.info("access password set up from %s", client_ip)
    return {"ok": True, "configured": True}


@router.post("/login")
def login(req: LoginIn, request: Request, response: Response) -> dict:
    """Authenticate an email account or a legacy password installation."""
    ip = _client_ip(request)
    _check_login_rate_limit(ip)

    if not auth.is_configured():
        raise HTTPException(status_code=409, detail="系统尚未创建账户")

    user = None
    if auth.has_users():
        if not req.email:
            raise HTTPException(status_code=400, detail="请输入邮箱地址")
        result = auth.authenticate_user(req.email, req.password)
        if result:
            token, user = result
        else:
            token = None
    else:
        token = auth.verify_and_create_session(req.password)
    if token is None:
        _record_login_fail(ip)
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    _clear_login_fails(ip)
    _set_session_cookie(response, request, token)
    return {
        "ok": True,
        "authenticated": True,
        "legacy_migration_required": user is None,
        "user": user,
    }


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    """注销当前会话。"""
    token = request.cookies.get(COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"ok": True}


@router.post("/change-password")
def change_password(req: ChangePasswordIn, request: Request) -> dict:
    """Change the current account password and revoke its sessions."""
    token = request.cookies.get(COOKIE_NAME)
    if not (token and auth.is_valid_session(token)):
        raise HTTPException(status_code=401, detail="请先登录")
    try:
        changed = auth.change_password(token, req.old_password, req.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not changed:
        ip = _client_ip(request)
        _record_login_fail(ip)
        raise HTTPException(status_code=400, detail="旧密码错误")
    return {"ok": True, "message": "密码已修改, 请重新登录"}
