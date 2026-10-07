"""Application liveness and readiness checks."""
from __future__ import annotations

import os
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.market_time import cn_today
from app.services.data_release import collect_dataset_snapshot, release_health
from app.services.provider_audit import audit_provider_routes

_DISK_WARNING_BYTES = 1024 * 1024 * 1024
_DISK_ERROR_BYTES = 100 * 1024 * 1024


def _check_data_dir(data_dir: Path) -> dict[str, Any]:
    exists = data_dir.exists() and data_dir.is_dir()
    writable = exists and os.access(data_dir, os.W_OK)
    if not exists:
        return {"status": "error", "exists": False, "writable": False}
    if not writable:
        return {"status": "error", "exists": True, "writable": False}
    return {"status": "ok", "exists": True, "writable": True}


def _check_disk(data_dir: Path) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(data_dir)
    except OSError as exc:
        return {"status": "error", "message": str(exc)}
    if usage.free < _DISK_ERROR_BYTES:
        status = "error"
    elif usage.free < _DISK_WARNING_BYTES:
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def _check_scheduler(app_state: object) -> dict[str, Any]:
    scheduler = getattr(app_state, "scheduler", None)
    if scheduler is None:
        return {"status": "error", "running": False, "jobs": 0}
    running = bool(getattr(scheduler, "running", False))
    try:
        jobs = len(scheduler.get_jobs())
    except Exception:
        jobs = None
    return {
        "status": "ok" if running else "error",
        "running": running,
        "jobs": jobs,
    }


def _check_data_freshness(data_dir: Path) -> dict[str, Any]:
    snapshot = collect_dataset_snapshot(data_dir)
    latest_text = snapshot["stock_enriched"].get("latest_partition")
    latest: date | None = None
    if isinstance(latest_text, str):
        try:
            latest = date.fromisoformat(latest_text)
        except ValueError:
            latest = None
    lag_days = (cn_today() - latest).days if latest is not None else None
    return {
        "status": "warning" if lag_days is None or lag_days > 7 else "ok",
        "stock_daily_latest": snapshot["stock_daily"].get("latest_partition"),
        "stock_enriched_latest": latest_text,
        "stock_minute_latest": snapshot["stock_minute"].get("latest_partition"),
        "auction_snapshot_latest": snapshot["auction_snapshot"].get("latest_partition"),
        "calendar_lag_days": lag_days,
    }


def _check_task_state() -> dict[str, Any]:
    try:
        from app.services.pipeline_jobs import job_store

        jobs = job_store.list_recent(limit=20)
        active_id = job_store.active_id()
    except Exception as exc:
        return {
            "status": "warning",
            "active_id": None,
            "last_pipeline": None,
            "message": str(exc),
        }

    last_pipeline = next(
        (
            job
            for job in jobs
            if job.get("kind") == "daily_pipeline"
            or "daily_days" in (job.get("result") or {})
        ),
        None,
    )
    failed = (
        last_pipeline is not None
        and last_pipeline.get("status") in {"failed", "interrupted"}
    )
    return {
        "status": "warning" if failed else "ok",
        "active_id": active_id,
        "last_pipeline": (
            {
                "id": last_pipeline.get("id") or last_pipeline.get("job_id"),
                "status": last_pipeline.get("status"),
                "finished_at": last_pipeline.get("finished_at"),
            }
            if last_pipeline is not None
            else None
        ),
    }


def readiness(app_state: object, data_dir: Path) -> dict[str, Any]:
    """Evaluate local dependencies without calling external providers."""
    repository_ready = getattr(app_state, "repo", None) is not None
    checks = {
        "repository": {
            "status": "ok" if repository_ready else "error",
            "initialized": repository_ready,
        },
        "data_dir": _check_data_dir(Path(data_dir)),
        "disk": _check_disk(Path(data_dir)),
        "scheduler": _check_scheduler(app_state),
        "providers": audit_provider_routes(),
        "data_freshness": _check_data_freshness(Path(data_dir)),
        "tasks": _check_task_state(),
        "data_release": release_health(Path(data_dir)),
    }
    if not bool(getattr(app_state, "indicators_ready", True)):
        checks["indicator_cache"] = {
            "status": "warning",
            "ready": False,
        }

    statuses = [str(check.get("status") or "error") for check in checks.values()]
    ready = "error" not in statuses
    status = "ok" if ready and "warning" not in statuses else "degraded"
    if not ready:
        status = "error"
    return {
        "status": status,
        "ready": ready,
        "checked_at": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "checks": checks,
    }
