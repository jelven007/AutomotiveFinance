"""Shared instrument-name normalization and current-list eligibility rules."""

from __future__ import annotations

SH_A_SHARE_PREFIXES = ("600", "601", "603", "605", "688", "689")
SZ_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301")


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


def current_a_share_identity(
    symbol: object = None,
    *,
    code: object = None,
    exchange: object = None,
) -> tuple[str, str, str] | None:
    """Normalize one supported current-pool stock identity.

    The production stock pool covers only Shanghai/Shenzhen main boards,
    STAR Market and ChiNext. ETFs, indices, B shares and other markets
    cannot match these exchange-specific prefixes.
    """
    symbol_text = str(symbol or "").strip().upper()
    symbol_code = ""
    symbol_exchange = ""
    if "." in symbol_text:
        symbol_code, symbol_exchange = symbol_text.rsplit(".", 1)

    normalized_code = str(code or symbol_code or symbol_text).strip()
    normalized_exchange = str(exchange or symbol_exchange).strip().upper()
    if not normalized_exchange:
        if normalized_code.startswith(SH_A_SHARE_PREFIXES):
            normalized_exchange = "SH"
        elif normalized_code.startswith(SZ_A_SHARE_PREFIXES):
            normalized_exchange = "SZ"

    prefixes = {
        "SH": SH_A_SHARE_PREFIXES,
        "SZ": SZ_A_SHARE_PREFIXES,
    }.get(normalized_exchange)
    if (
        prefixes is None
        or len(normalized_code) != 6
        or not normalized_code.isdigit()
        or not normalized_code.startswith(prefixes)
    ):
        return None
    return (
        f"{normalized_code}.{normalized_exchange}",
        normalized_code,
        normalized_exchange,
    )


def is_current_a_share(
    symbol: object = None,
    *,
    code: object = None,
    exchange: object = None,
) -> bool:
    return current_a_share_identity(
        symbol,
        code=code,
        exchange=exchange,
    ) is not None
