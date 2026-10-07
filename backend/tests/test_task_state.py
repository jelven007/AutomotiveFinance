from __future__ import annotations

import pytest

from app.services.task_state import (
    ACTIVE_TASK_STATUSES,
    TERMINAL_TASK_STATUSES,
    InvalidTaskStatusTransitionError,
    TaskStateValidationError,
    normalize_task_record,
    normalize_task_status,
    transition_task_record,
)


def _record(status: str = "queued") -> dict:
    return {
        "id": "job-1",
        "status": status,
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-01T00:00:00+00:00",
        "started_at": None,
        "finished_at": None,
        "cancellation_requested_at": None,
        "error": None,
    }


def test_legacy_pending_normalizes_to_queued() -> None:
    assert normalize_task_status("pending") == "queued"
    assert normalize_task_record(_record("pending"))["status"] == "queued"


def test_lifecycle_timestamps_are_applied_consistently() -> None:
    running = transition_task_record(
        _record(),
        "running",
        now="2026-07-01T00:01:00+00:00",
    )
    cancelling = transition_task_record(
        running,
        "cancelling",
        now="2026-07-01T00:02:00+00:00",
    )
    cancelled = transition_task_record(
        cancelling,
        "cancelled",
        now="2026-07-01T00:03:00+00:00",
    )

    assert running["started_at"] == "2026-07-01T00:01:00+00:00"
    assert cancelling["cancellation_requested_at"] == "2026-07-01T00:02:00+00:00"
    assert cancelled["finished_at"] == "2026-07-01T00:03:00+00:00"


def test_terminal_state_cannot_restart() -> None:
    failed = transition_task_record(_record(), "failed", error="boom")

    with pytest.raises(InvalidTaskStatusTransitionError):
        transition_task_record(failed, "running")


def test_status_sets_are_disjoint_and_complete() -> None:
    assert ACTIVE_TASK_STATUSES.isdisjoint(TERMINAL_TASK_STATUSES)
    assert "cancelling" in ACTIVE_TASK_STATUSES
    assert {"cancelled", "interrupted", "failed"} <= TERMINAL_TASK_STATUSES


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(TaskStateValidationError):
        normalize_task_status("mystery")
