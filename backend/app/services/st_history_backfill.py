"""Backfill point-in-time ST/*ST intervals from TDX F10 data."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from app.instrument_history import HISTORY_SCHEMA, HISTORY_SCHEMA_VERSION

_SECTION_MARKER = "【特别处理】"
_DATE_FIELDS = ("公告日期", "实施日期")
_REMOVAL_WORDS = ("撤销", "撤消", "取消", "摘帽")
_KEEP_WARNING_WORDS = (
    "实施其他",
    "继续实施其他",
    "保留其他",
    "实施退市",
    "继续实施退市",
    "保留退市",
)
_NO_STATE_CHANGE_TYPES = {
    "暂停上市",
    "恢复上市",
    "终止上市",
    "退市整理期",
}

RAW_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "text": pl.String,
    "fetched_at": pl.String,
    "content_sha256": pl.String,
    "error": pl.String,
}
EVENT_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "announcement_date": pl.Date,
    "effective_date": pl.Date,
    "treatment_type": pl.String,
    "other": pl.String,
    "is_risk_warning_after": pl.Boolean,
    "classification": pl.String,
}


def parse_special_treatment_events(symbol: str, text: str) -> list[dict[str, Any]]:
    """Parse the structured ``特别处理`` blocks from one TDX F10 page."""
    marker_at = text.find(_SECTION_MARKER)
    if marker_at < 0:
        raise ValueError("F10 最新提示缺少特别处理栏目")
    section = text[marker_at + len(_SECTION_MARKER):]
    if "暂无数据" in section[:200]:
        return []

    starts = [
        match.start()
        for match in re.finditer(r"\uff5c\s*公告日期\s*\uff5c", section)
    ]
    events: list[dict[str, Any]] = []
    for index, start in enumerate(starts):
        stop = starts[index + 1] if index + 1 < len(starts) else len(section)
        block = section[start:stop]
        announcement = _parse_date_field(block, "公告日期")
        effective = _parse_date_field(block, "实施日期")
        treatment_type = _parse_text_field(block, "处理类型")
        other = _parse_text_field(block, "其他")
        if announcement is None or effective is None or not treatment_type:
            raise ValueError(f"{symbol} 特别处理记录字段不完整")
        state, classification = classify_treatment(treatment_type, other)
        events.append(
            {
                "symbol": symbol,
                "announcement_date": announcement,
                "effective_date": effective,
                "treatment_type": treatment_type,
                "other": other,
                "is_risk_warning_after": state,
                "classification": classification,
            }
        )
    if not starts:
        raise ValueError(f"{symbol} 特别处理栏目既非暂无数据也无事件")
    return events


def classify_treatment(
    treatment_type: str,
    other: str = "",
) -> tuple[bool | None, str]:
    """Return the post-event risk-warning state.

    ``None`` means the event affects listing state but does not itself change
    ST/*ST status. Ambiguous removal wording is handled fail-closed.
    """
    kind = _compact(treatment_type).replace("撤消", "撤销")
    detail = _compact(other).replace("撤消", "撤销")
    combined = f"{kind}{detail}"
    if kind in _NO_STATE_CHANGE_TYPES:
        return None, "no_state_change"

    removing = any(word in combined for word in _REMOVAL_WORDS)
    if removing:
        if any(word in combined for word in _KEEP_WARNING_WORDS):
            return True, "warning_retained"
        if _has_positive_st_marker_after_removal(detail):
            return True, "warning_retained"
        return False, "warning_removed"

    if (
        "ST" in kind.upper()
        or "退市风险警示" in kind
        or "其他风险警示" in kind
        or "特别处理" in kind
    ):
        return True, "warning_applied"
    raise ValueError(f"无法分类的特别处理类型: {treatment_type!r} / {other!r}")


def build_governed_history(
    instruments: pl.DataFrame,
    coverage: pl.DataFrame,
    events: pl.DataFrame,
    *,
    successful_symbols: set[str],
    quarantined_symbols: set[str],
    as_of: date,
    available_at: datetime | str | None = None,
    source_prefix: str = "mootdx_f10",
) -> pl.DataFrame:
    """Build SCD2 status intervals covering every symbol with daily data.

    Fetch/parse failures are quarantined as risk-warning for their entire local
    history. This intentionally prefers false exclusions over ST leakage.
    """
    required_coverage = {"symbol", "first_date"}
    if not required_coverage <= set(coverage.columns):
        raise ValueError(f"coverage missing columns: {sorted(required_coverage - set(coverage.columns))}")
    if not {"symbol", "name"} <= set(instruments.columns):
        raise ValueError("instruments must contain symbol and name")

    fetched = successful_symbols | quarantined_symbols
    covered_symbols = set(coverage["symbol"].cast(pl.String).to_list())
    missing = covered_symbols - fetched
    if missing:
        raise ValueError(f"status governance missing {len(missing)} covered symbols")

    names = {
        str(row["symbol"]): str(row["name"] or "")
        for row in instruments.select("symbol", "name").iter_rows(named=True)
    }
    first_dates = {
        str(row["symbol"]): row["first_date"]
        for row in coverage.select("symbol", "first_date").iter_rows(named=True)
    }
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not events.is_empty():
        for row in events.sort(["symbol", "effective_date", "announcement_date"]).iter_rows(named=True):
            by_symbol[str(row["symbol"])].append(row)

    observed_at = _available_at_text(available_at)
    rows: list[dict[str, Any]] = []
    for symbol in sorted(covered_symbols):
        first_date = first_dates[symbol]
        current_name = names.get(symbol, "")
        current_risk = "ST" in current_name.upper()
        if symbol in quarantined_symbols:
            states = [(first_date, True, f"{source_prefix}_unknown_quarantine")]
        else:
            state = False
            for event in by_symbol.get(symbol, []):
                if event["effective_date"] >= first_date:
                    break
                if event["is_risk_warning_after"] is not None:
                    state = bool(event["is_risk_warning_after"])
            states = [(first_date, state, f"{source_prefix}_backfill")]
            for event in by_symbol.get(symbol, []):
                effective = event["effective_date"]
                next_state = event["is_risk_warning_after"]
                if effective < first_date or effective > as_of or next_state is None:
                    continue
                next_state = bool(next_state)
                if next_state != states[-1][1]:
                    states.append((effective, next_state, f"{source_prefix}_backfill"))
            if states[-1][1] != current_risk:
                snapshot_prefix = source_prefix.removesuffix("_f10")
                states.append((as_of, current_risk, f"{snapshot_prefix}_snapshot_reconcile"))

        for index, (valid_from, risk, source) in enumerate(states):
            valid_to = states[index + 1][0] if index + 1 < len(states) else None
            if valid_to is not None and valid_to <= valid_from:
                continue
            rows.append(
                {
                    "schema_version": HISTORY_SCHEMA_VERSION,
                    "symbol": symbol,
                    "valid_from": valid_from,
                    "valid_to": valid_to,
                    "available_at": observed_at,
                    "source": source,
                    # F10 gives exact risk dates but not a dated short-name
                    # mapping. Keep the current display name while governing
                    # the risk field independently.
                    "name": current_name,
                    "is_risk_warning": risk,
                    "is_listed": True,
                    "listing_date": first_date,
                }
            )
    return pl.DataFrame(rows, schema=HISTORY_SCHEMA).sort(["symbol", "valid_from"])


def events_frame(records: list[dict[str, Any]]) -> pl.DataFrame:
    if not records:
        return pl.DataFrame(schema=EVENT_SCHEMA)
    return pl.DataFrame(records, schema=EVENT_SCHEMA).sort(
        ["symbol", "effective_date", "announcement_date"]
    )


def _parse_date_field(block: str, label: str) -> date | None:
    value = _parse_text_field(block, label)
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_text_field(block: str, label: str) -> str:
    match = re.search(
        rf"\uff5c\s*{re.escape(label)}\s*\uff5c\s*"
        rf"([^\uff5c\r\n]*?)\s*\uff5c",
        block,
    )
    return match.group(1).strip() if match else ""


def _has_positive_st_marker_after_removal(detail: str) -> bool:
    if not detail:
        return False
    for part in re.split(r"[+\u3001\uff0c,;/]", detail):
        normalized = _compact(part)
        if not normalized or any(word in normalized for word in _REMOVAL_WORDS):
            continue
        if (
            "ST" in normalized.upper()
            or "实施其他" in normalized
            or "实施退市" in normalized
        ):
            return True
    return False


def _compact(value: object) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _available_at_text(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)
