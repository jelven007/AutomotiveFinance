from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.services.st_history_backfill import (
    build_governed_history,
    classify_treatment,
    events_frame,
    parse_special_treatment_events,
)
from scripts.analyze_market_waves import confirmed_zigzag_pivots


def test_parse_special_treatment_events_and_removal_semantics() -> None:
    text = """
【特别处理】
|公告日期        |2021-05-26|
|实施日期        |2021-05-27|
|处理类型        |撤消特别处理|
|其他            |-|
|公告日期        |2020-06-18|
|实施日期        |2020-06-19|
|处理类型        |撤销退市风险实施其他|
|其他            |-|
|公告日期        |2019-04-23|
|实施日期        |2019-04-24|
|处理类型        |*ST|
|其他            |-|
""".replace("|", "\uff5c")

    rows = parse_special_treatment_events("000572.SZ", text)

    assert [row["effective_date"] for row in rows] == [
        date(2021, 5, 27),
        date(2020, 6, 19),
        date(2019, 4, 24),
    ]
    assert [row["is_risk_warning_after"] for row in rows] == [False, True, True]


@pytest.mark.parametrize(
    ("kind", "other", "expected"),
    [
        ("ST", "-", True),
        ("*ST", "-", True),
        ("撤销退市风险警示", "撤销*ST+撤销ST", False),
        ("撤销退市风险实施其他", "-", True),
        ("暂停上市", "-", None),
        ("恢复上市", "-", None),
    ],
)
def test_classify_treatment(kind: str, other: str, expected: bool | None) -> None:
    assert classify_treatment(kind, other)[0] is expected


def test_unknown_treatment_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="无法分类"):
        classify_treatment("未知处理")


def test_build_governed_history_tracks_events_and_quarantines_unknowns() -> None:
    instruments = pl.DataFrame(
        {
            "symbol": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "name": ["正常一", "*ST当前", "正常三"],
        }
    )
    coverage = pl.DataFrame(
        {
            "symbol": ["000001.SZ", "000002.SZ", "000003.SZ"],
            "first_date": [date(2016, 1, 4)] * 3,
        }
    )
    events = events_frame(
        [
            {
                "symbol": "000001.SZ",
                "announcement_date": date(2019, 4, 22),
                "effective_date": date(2019, 4, 23),
                "treatment_type": "*ST",
                "other": "-",
                "is_risk_warning_after": True,
                "classification": "warning_applied",
            },
            {
                "symbol": "000001.SZ",
                "announcement_date": date(2021, 5, 26),
                "effective_date": date(2021, 5, 27),
                "treatment_type": "撤消特别处理",
                "other": "-",
                "is_risk_warning_after": False,
                "classification": "warning_removed",
            },
            {
                "symbol": "000002.SZ",
                "announcement_date": date(2020, 4, 22),
                "effective_date": date(2020, 4, 23),
                "treatment_type": "ST",
                "other": "-",
                "is_risk_warning_after": True,
                "classification": "warning_applied",
            },
        ]
    )

    history = build_governed_history(
        instruments,
        coverage,
        events,
        successful_symbols={"000001.SZ", "000002.SZ"},
        quarantined_symbols={"000003.SZ"},
        as_of=date(2026, 10, 8),
        available_at="2026-10-08T04:00:00+00:00",
    )

    first = history.filter(pl.col("symbol") == "000001.SZ")
    assert first["valid_from"].to_list() == [
        date(2016, 1, 4),
        date(2019, 4, 23),
        date(2021, 5, 27),
    ]
    assert first["is_risk_warning"].to_list() == [False, True, False]
    second = history.filter(pl.col("symbol") == "000002.SZ")
    assert second["is_risk_warning"].to_list() == [False, True]
    quarantined = history.filter(pl.col("symbol") == "000003.SZ").row(0, named=True)
    assert quarantined["is_risk_warning"] is True
    assert quarantined["source"] == "rustdx_f10_unknown_quarantine"

    archived_history = build_governed_history(
        instruments, coverage, events,
        successful_symbols={"000001.SZ", "000002.SZ"},
        quarantined_symbols={"000003.SZ"},
        as_of=date(2026, 10, 8),
        available_at="2026-10-08T04:00:00+00:00",
        source_prefix="archived_f10",
    )
    assert archived_history.drop("source").equals(history.drop("source"))
    assert set(archived_history["source"].to_list()) == {
        "archived_f10_backfill", "archived_f10_unknown_quarantine",
    }


def test_zigzag_only_returns_reversal_confirmed_pivots() -> None:
    values = pl.Series([100.0, 120.0, 105.0, 140.0, 130.0]).to_numpy()

    assert confirmed_zigzag_pivots(values, 0.10) == [0, 1, 2]
