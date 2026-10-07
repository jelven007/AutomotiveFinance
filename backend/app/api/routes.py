"""Core liveness, readiness, and capability routes."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import settings
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities, tier_label

router = APIRouter()


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        # 三态: none(无key/无效) / free(免费key) / api_key(付费档)
        "mode": tf_client.current_mode(),
    }


@router.get("/health/live")
def health_live() -> dict:
    """Process liveness only; external dependencies are intentionally excluded."""
    return {
        "status": "ok",
        "version": __version__,
    }


def _readiness_response(request: Request) -> JSONResponse:
    from app.services.health import readiness

    data_dir = getattr(
        getattr(getattr(request.app.state, "repo", None), "store", None),
        "data_dir",
        settings.data_dir,
    )
    payload = readiness(request.app.state, data_dir)
    payload["version"] = __version__
    payload["mode"] = tf_client.current_mode()
    return JSONResponse(
        status_code=200 if payload["ready"] else 503,
        content=payload,
    )


@router.get("/health/ready")
def health_ready(request: Request) -> JSONResponse:
    """Deep local readiness check for orchestrators and operators."""
    return _readiness_response(request)


@router.get("/api/health")
def api_health(request: Request) -> JSONResponse:
    """Authenticated-product alias kept public by the API gateway whitelist."""
    return _readiness_response(request)


@router.get("/api/capabilities")
def capabilities() -> dict:
    """前端用来决定哪些功能可用、哪些灰显。"""
    capset = detect_capabilities()
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }


@router.post("/api/capabilities/redetect")
def redetect(request: Request) -> dict:
    """用户在设置页"重新检测"按钮。"""
    capset = detect_capabilities(force=True)
    # 同步刷新 app.state 快照 (minute_refresh 等服务的门控读这里) 与财务调度器,
    # 与 settings.py 各探测路径一致 — 否则重检测后服务侧仍读旧 capset 被错误门控
    request.app.state.capabilities = capset
    from app.api.settings import _sync_financial_scheduler_caps
    _sync_financial_scheduler_caps(request.app.state, capset)
    return {
        "label": tier_label(),
        "capabilities": capset.to_dict(),
    }
