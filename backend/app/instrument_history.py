"""Point-in-time instrument status history.

The current ``instruments`` table remains the fast lookup snapshot. This module
stores semantic status changes as SCD2 intervals so historical calculations do
not have to reuse today's name or risk-warning state.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.services.fs_utils import atomic_write_parquet

HISTORY_RELATIVE_PATH = Path("instrument_status") / "history.parquet"
HISTORY_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String,
    "valid_from": pl.Date,
    "valid_to": pl.Date,
    "available_at": pl.String,
    "source": pl.String,
    "name": pl.String,
    "is_risk_warning": pl.Boolean,
    "is_listed": pl.Boolean,
    "listing_date": pl.Date,
}
_STATE_FIELDS = ("name", "is_risk_warning", "is_listed", "listing_date")
_PIT_FIELDS = {
    "name": "_pit_name",
    "is_risk_warning": "_pit_is_risk_warning",
    "is_listed": "_pit_is_listed",
    "listing_date": "_pit_listing_date",
    "available_at": "_pit_available_at",
    "source": "_pit_source",
}


class InstrumentHistoryError(RuntimeError):
    """Raised when persisted instrument history cannot be trusted."""


def history_path(data_dir: Path | str) -> Path:
    return Path(data_dir) / HISTORY_RELATIVE_PATH


def empty_instrument_history() -> pl.DataFrame:
    return pl.DataFrame(schema=HISTORY_SCHEMA)


def load_instrument_history(data_dir: Path | str) -> pl.DataFrame:
    """Load and validate the SCD2 history; absence is a valid empty state."""
    path = history_path(data_dir)
    if not path.exists():
        return empty_instrument_history()
    try:
        frame = pl.read_parquet(path)
        return _normalize_history(frame)
    except InstrumentHistoryError:
        raise
    except Exception as exc:
        raise InstrumentHistoryError(
            f"instrument status history is unreadable: {path}"
        ) from exc


def update_instrument_history(
    data_dir: Path | str,
    instruments: pl.DataFrame,
    *,
    as_of: date,
    source: str,
    available_at: datetime | str | None = None,
) -> int:
    """Merge one observed current snapshot into the SCD2 history.

    ``valid_to`` is exclusive. Instruments missing from a later successful
    snapshot receive an explicit unlisted tombstone, preventing a previous
    active row from leaking into later dates.
    """
    current = _normalize_current(instruments)
    if current.is_empty():
        return 0

    observed_at = _available_at_text(available_at)
    existing = load_instrument_history(data_dir)
    latest: date | None = None
    if not existing.is_empty():
        latest = existing["valid_from"].max()
        if latest is not None and as_of < latest:
            raise InstrumentHistoryError(
                f"instrument status snapshot {as_of} predates existing history {latest}"
            )
        if latest == as_of:
            existing = _rollback_same_day(existing, as_of)

    open_rows = (
        existing.filter(pl.col("valid_to").is_null())
        if not existing.is_empty()
        else empty_instrument_history()
    )
    previous = {
        str(row["symbol"]): row
        for row in open_rows.iter_rows(named=True)
    }
    observed = {
        str(row["symbol"]): row
        for row in current.iter_rows(named=True)
    }

    close_symbols: set[str] = set()
    additions: list[dict[str, Any]] = []
    for symbol, row in observed.items():
        prior = previous.get(symbol)
        effective_name = row["name"] or (prior["name"] if prior is not None else "")
        state = {
            "name": effective_name,
            "is_risk_warning": "ST" in effective_name.upper(),
            "is_listed": True,
            "listing_date": (
                row["listing_date"]
                or (prior["listing_date"] if prior is not None else None)
            ),
        }
        if prior is not None and _same_state(prior, state):
            continue
        if prior is not None:
            close_symbols.add(symbol)
        additions.append(
            _history_row(
                symbol,
                state,
                as_of=as_of,
                source=source,
                available_at=observed_at,
            )
        )

    for symbol, prior in previous.items():
        if symbol in observed or not prior["is_listed"]:
            continue
        close_symbols.add(symbol)
        additions.append(
            _history_row(
                symbol,
                {
                    "name": prior["name"],
                    "is_risk_warning": prior["is_risk_warning"],
                    "is_listed": False,
                    "listing_date": prior["listing_date"],
                },
                as_of=as_of,
                source=source,
                available_at=observed_at,
            )
        )

    if not additions and latest != as_of:
        return 0

    merged = existing
    if close_symbols and not merged.is_empty():
        merged = merged.with_columns(
            pl.when(
                pl.col("valid_to").is_null()
                & pl.col("symbol").is_in(sorted(close_symbols))
            )
            .then(pl.lit(as_of))
            .otherwise(pl.col("valid_to"))
            .alias("valid_to")
        )
    if additions:
        added = pl.DataFrame(additions, schema=HISTORY_SCHEMA)
        merged = (
            added
            if merged.is_empty()
            else pl.concat([merged, added], how="vertical_relaxed")
        )
    merged = _normalize_history(merged).sort(["symbol", "valid_from"])
    path = history_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(merged, path)
    return len(additions)


def attach_instrument_history(
    rows: pl.DataFrame,
    history: pl.DataFrame | None,
) -> pl.DataFrame:
    """Attach the status known on each row date without looking into the future.

    Added columns use the ``_pit_`` prefix. ``_pit_known`` is false before the
    first captured state for a symbol; callers may then choose an explicit
    compatibility fallback instead of mistaking current metadata for history.
    """
    if rows.is_empty() or not {"symbol", "date"} <= set(rows.columns):
        return rows
    if history is None or history.is_empty():
        return _add_empty_pit_columns(rows)

    normalized = _normalize_history(history)
    status = normalized.select(
        pl.col("symbol"),
        pl.col("valid_from").alias("_pit_valid_from"),
        pl.col("valid_to").alias("_pit_valid_to"),
        *[
            pl.col(source).alias(target)
            for source, target in _PIT_FIELDS.items()
        ],
    ).sort(["symbol", "_pit_valid_from"])
    resolved = (
        rows
        .with_row_index("_pit_row_order")
        .with_columns(
            pl.col("symbol").cast(pl.String),
            pl.col("date").cast(pl.Date, strict=False),
        )
        .sort(["symbol", "date"])
        .join_asof(
            status,
            left_on="date",
            right_on="_pit_valid_from",
            by="symbol",
            strategy="backward",
            check_sortedness=False,
        )
    )
    interval_valid = (
        pl.col("_pit_valid_from").is_not_null()
        & (
            pl.col("_pit_valid_to").is_null()
            | (pl.col("date") < pl.col("_pit_valid_to"))
        )
    )
    resolved = resolved.with_columns(interval_valid.alias("_pit_known"))
    resolved = resolved.with_columns(
        [
            pl.when(pl.col("_pit_known"))
            .then(pl.col(target))
            .otherwise(None)
            .alias(target)
            for target in _PIT_FIELDS.values()
        ]
    )
    return resolved.sort("_pit_row_order").drop("_pit_row_order", "_pit_valid_to")


def instrument_status_on(
    data_dir: Path | str,
    symbol: str,
    as_of: date,
) -> dict[str, Any] | None:
    """Return the status effective on ``as_of`` or ``None`` when unknown."""
    history = load_instrument_history(data_dir)
    if history.is_empty():
        return None
    matches = history.filter(
        (pl.col("symbol") == symbol)
        & (pl.col("valid_from") <= as_of)
        & (
            pl.col("valid_to").is_null()
            | (pl.col("valid_to") > as_of)
        )
    ).sort("valid_from")
    return matches.row(-1, named=True) if not matches.is_empty() else None


def pit_columns() -> tuple[str, ...]:
    return ("_pit_valid_from", "_pit_known", *_PIT_FIELDS.values())


def _normalize_current(instruments: pl.DataFrame) -> pl.DataFrame:
    if instruments.is_empty() or "symbol" not in instruments.columns:
        return pl.DataFrame(
            schema={
                "symbol": pl.String,
                "name": pl.String,
                "is_risk_warning": pl.Boolean,
                "listing_date": pl.Date,
            }
        )
    name = (
        pl.col("name").cast(pl.String, strict=False).fill_null("")
        if "name" in instruments.columns
        else pl.lit("").cast(pl.String)
    )
    listing_date = (
        _date_expr(instruments, "listing_date")
        if "listing_date" in instruments.columns
        else pl.lit(None).cast(pl.Date)
    )
    return (
        instruments
        .select(
            pl.col("symbol").cast(pl.String),
            name.str.replace_all("\x00", "").str.strip_chars().alias("name"),
            listing_date.alias("listing_date"),
        )
        .filter(pl.col("symbol").is_not_null() & (pl.col("symbol") != ""))
        .with_columns(
            pl.col("name").str.to_uppercase().str.contains("ST", literal=True)
            .alias("is_risk_warning")
        )
        .unique(subset=["symbol"], keep="last")
        .sort("symbol")
    )


def _normalize_history(frame: pl.DataFrame) -> pl.DataFrame:
    required = {"symbol", "valid_from", "valid_to", "name", "is_risk_warning", "is_listed"}
    missing = required - set(frame.columns)
    if missing:
        raise InstrumentHistoryError(
            f"instrument status history missing columns: {sorted(missing)}"
        )
    additions: list[pl.Expr] = []
    for name, dtype in HISTORY_SCHEMA.items():
        if name not in frame.columns:
            additions.append(pl.lit(None).cast(dtype).alias(name))
    if additions:
        frame = frame.with_columns(additions)
    normalized = frame.select(
        pl.col("symbol").cast(pl.String),
        _date_expr(frame, "valid_from").alias("valid_from"),
        _date_expr(frame, "valid_to").alias("valid_to"),
        pl.col("available_at").cast(pl.String, strict=False),
        pl.col("source").cast(pl.String, strict=False),
        pl.col("name").cast(pl.String, strict=False).fill_null(""),
        pl.col("is_risk_warning").cast(pl.Boolean, strict=False).fill_null(False),
        pl.col("is_listed").cast(pl.Boolean, strict=False).fill_null(False),
        _date_expr(frame, "listing_date").alias("listing_date"),
    ).filter(
        pl.col("symbol").is_not_null()
        & pl.col("valid_from").is_not_null()
    )
    duplicates = (
        normalized.group_by(["symbol", "valid_from"]).len()
        .filter(pl.col("len") > 1)
    )
    if not duplicates.is_empty():
        raise InstrumentHistoryError("instrument status history contains duplicate versions")
    return normalized.sort(["symbol", "valid_from"])


def _rollback_same_day(history: pl.DataFrame, as_of: date) -> pl.DataFrame:
    return (
        history
        .filter(pl.col("valid_from") != as_of)
        .with_columns(
            pl.when(pl.col("valid_to") == as_of)
            .then(pl.lit(None).cast(pl.Date))
            .otherwise(pl.col("valid_to"))
            .alias("valid_to")
        )
    )


def _history_row(
    symbol: str,
    state: dict[str, Any],
    *,
    as_of: date,
    source: str,
    available_at: str,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "valid_from": as_of,
        "valid_to": None,
        "available_at": available_at,
        "source": str(source or "unknown"),
        **state,
    }


def _same_state(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    return all(previous.get(field) == current.get(field) for field in _STATE_FIELDS)


def _available_at_text(value: datetime | str | None) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)


def _date_expr(frame: pl.DataFrame, column: str) -> pl.Expr:
    dtype = frame.schema[column]
    if dtype == pl.String:
        return pl.col(column).str.to_date(strict=False)
    return pl.col(column).cast(pl.Date, strict=False)


def _add_empty_pit_columns(rows: pl.DataFrame) -> pl.DataFrame:
    return rows.with_columns(
        pl.lit(None).cast(pl.Date).alias("_pit_valid_from"),
        pl.lit(False).alias("_pit_known"),
        pl.lit(None).cast(pl.String).alias("_pit_name"),
        pl.lit(None).cast(pl.Boolean).alias("_pit_is_risk_warning"),
        pl.lit(None).cast(pl.Boolean).alias("_pit_is_listed"),
        pl.lit(None).cast(pl.Date).alias("_pit_listing_date"),
        pl.lit(None).cast(pl.String).alias("_pit_available_at"),
        pl.lit(None).cast(pl.String).alias("_pit_source"),
    )
