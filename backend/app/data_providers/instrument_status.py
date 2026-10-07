"""Shared instrument-name normalization and current-list eligibility rules."""

from __future__ import annotations


def normalize_instrument_name(value: object) -> str:
    """Remove protocol padding without altering meaningful inner characters."""
    return str(value or "").replace("\x00", "").strip()


def is_delisted_name(value: object) -> bool:
    """Return True for exchange delisting/termination labels.

    Current SH/SZ collection is intentionally survivor-only. Delisting-period
    labels commonly end in ``退``; ``退市`` and ``摘牌`` cover older variants.
    """
    name = normalize_instrument_name(value)
    return bool(name) and (
        name.endswith("退")
        or name.startswith("退市")
        or "摘牌" in name
    )
