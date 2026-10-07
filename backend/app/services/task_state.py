"""Shared lifecycle rules for durable background tasks."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

TaskStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "succeeded_with_budget_exhausted",
    "failed",
    "cancelled",
    "interrupted",
    "skipped_prerequisite",
]

TASK_STATUSES: frozenset[str] = frozenset(
    {
        "queued",
        "running",
        "cancelling",
        "succeeded",
        "succeeded_with_budget_exhausted",
        "failed",
        "cancelled",
        "interrupted",
        "skipped_prerequisite",
    }
)
ACTIVE_TASK_STATUSES: frozenset[str] = frozenset(
    {"queued", "running", "cancelling"}
)
SUCCESS_TASK_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "succeeded_with_budget_exhausted"}
)
TERMINAL_TASK_STATUSES: frozenset[str] = TASK_STATUSES - ACTIVE_TASK_STATUSES

ALLOWED_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset(
        {
            "running",
            "cancelling",
            "cancelled",
            "failed",
            "interrupted",
            "skipped_prerequisite",
        }
    ),
    "running": frozenset(
        {
            "cancelling",
            "succeeded",
            "succeeded_with_budget_exhausted",
            "failed",
            "cancelled",
            "interrupted",
            "skipped_prerequisite",
        }
    ),
    "cancelling": frozenset(
        {
            "succeeded",
            "succeeded_with_budget_exhausted",
            "failed",
            "cancelled",
            "interrupted",
        }
    ),
    "succeeded": frozenset(),
    "succeeded_with_budget_exhausted": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "interrupted": frozenset(),
    "skipped_prerequisite": frozenset(),
}

_LEGACY_STATUSES = {"pending": "queued"}
_UNSET = object()


class TaskStateError(RuntimeError):
    pass


class TaskStateValidationError(TaskStateError, ValueError):
    pass


class InvalidTaskStatusTransitionError(TaskStateError):
    pass


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_task_status(value: object) -> TaskStatus:
    status = _LEGACY_STATUSES.get(str(value), str(value))
    if status not in TASK_STATUSES:
        raise TaskStateValidationError(f"unsupported task status: {value!r}")
    return status  # type: ignore[return-value]


def normalize_task_record(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with canonical status and lifecycle timestamps."""
    normalized = dict(record)
    normalized["status"] = normalize_task_status(
        normalized.get("status", "queued")
    )
    normalized.setdefault("schema_version", 1)
    normalized.setdefault("created_at", normalized.get("started_at"))
    normalized.setdefault("updated_at", normalized.get("created_at"))
    normalized.setdefault("started_at", None)
    normalized.setdefault("finished_at", None)
    normalized.setdefault("cancellation_requested_at", None)
    normalized.setdefault("error", None)
    return normalized


def transition_task_record(
    record: dict[str, Any],
    status: TaskStatus,
    *,
    error: str | None | object = _UNSET,
    now: str | None = None,
) -> dict[str, Any]:
    """Apply one validated transition to a copy of ``record``."""
    target = normalize_task_status(status)
    result = normalize_task_record(record)
    current = normalize_task_status(result["status"])
    if current == target:
        return result
    if target not in ALLOWED_TASK_TRANSITIONS[current]:
        raise InvalidTaskStatusTransitionError(
            f"cannot transition from {current} to {target}"
        )

    timestamp = now or utc_now_iso()
    result["status"] = target
    result["updated_at"] = timestamp
    if target == "running" and not result.get("started_at"):
        result["started_at"] = timestamp
    if target == "cancelling":
        result["cancellation_requested_at"] = timestamp
    if target in TERMINAL_TASK_STATUSES:
        result["finished_at"] = timestamp
    if error is not _UNSET:
        result["error"] = None if error is None else str(error)
    elif target in SUCCESS_TASK_STATUSES:
        result["error"] = None
    return result
