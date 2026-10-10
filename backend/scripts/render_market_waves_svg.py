#!/usr/bin/env python
"""Render a publication-style swing chart from the governed wave report."""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

import polars as pl

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
REPORT_DIR = (
    DATA_DIR / "reports" / "market-waves-20160101-20260930-st-governed"
)
OUTPUT = REPORT_DIR / "market-waves-professional.svg"

WIDTH = 1800
HEIGHT = 1320
LEFT = 110
RIGHT = 1740
TOP = 105
BOTTOM = 750
TABLE_TOP = 845

COLORS = {
    "bg": "#0B0E11",
    "panel": "#11161C",
    "panel_alt": "#151B22",
    "grid": "#26313B",
    "text": "#E6EDF3",
    "muted": "#93A1AE",
    "base": "#6D7B88",
    "up": "#F04452",
    "down": "#00B578",
    "border": "#2E3A45",
}


def _esc(value: object) -> str:
    return html.escape(str(value))


def _text(
    x: float,
    y: float,
    value: object,
    *,
    size: int = 18,
    color: str = COLORS["text"],
    anchor: str = "start",
    weight: int = 400,
    family: str = "Arial, PingFang SC, sans-serif",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{color}" '
        f'font-family="{family}" font-size="{size}" font-weight="{weight}" '
        f'text-anchor="{anchor}">{_esc(value)}</text>'
    )


def render() -> Path:
    daily = (
        pl.scan_parquet(
            str(DATA_DIR / "kline_index_daily" / "date=*" / "part.parquet"),
            hive_partitioning=False,
            missing_columns="insert",
            extra_columns="ignore",
        )
        .filter(
            (pl.col("symbol") == "000001.SH")
            & (pl.col("date") <= date(2026, 9, 30))
        )
        .select("date", "close")
        .sort("date")
        .collect()
    )
    waves = pl.read_parquet(REPORT_DIR / "waves.parquet").sort("wave_id")
    rows = waves.to_dicts()
    start_date = daily["date"].min()
    end_date = daily["date"].max()
    y_min = 2300.0
    y_max = 4300.0

    def x_of(value: date) -> float:
        ratio = (value - start_date).days / (end_date - start_date).days
        return LEFT + ratio * (RIGHT - LEFT)

    def y_of(value: float) -> float:
        ratio = (value - y_min) / (y_max - y_min)
        return BOTTOM - ratio * (BOTTOM - TOP)

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{COLORS["bg"]}"/>',
        _text(
            LEFT,
            48,
            "上证指数大级别 Swing / ZigZag 波段",
            size=30,
            weight=700,
        ),
        _text(
            LEFT,
            78,
            "SSE Composite · Daily · 12% reversal filter · "
            "2016-01-04 - 2026-09-30",
            size=17,
            color=COLORS["muted"],
        ),
        f'<rect x="{LEFT}" y="{TOP}" width="{RIGHT-LEFT}" '
        f'height="{BOTTOM-TOP}" rx="8" fill="{COLORS["panel"]}" '
        f'stroke="{COLORS["border"]}"/>',
    ]

    for level in range(2500, 4251, 250):
        y = y_of(level)
        out.append(
            f'<line x1="{LEFT}" y1="{y:.1f}" x2="{RIGHT}" y2="{y:.1f}" '
            f'stroke="{COLORS["grid"]}" stroke-width="1"/>'
        )
        out.append(
            _text(
                LEFT - 14,
                y + 6,
                f"{level:,}",
                size=15,
                color=COLORS["muted"],
                anchor="end",
            )
        )

    for year in range(2016, 2027):
        tick = max(start_date, date(year, 1, 1))
        x = x_of(tick)
        out.append(
            f'<line x1="{x:.1f}" y1="{TOP}" x2="{x:.1f}" y2="{BOTTOM}" '
            f'stroke="{COLORS["grid"]}" stroke-width="1"/>'
        )
        out.append(
            _text(
                x,
                BOTTOM + 30,
                year,
                size=15,
                color=COLORS["muted"],
                anchor="middle",
            )
        )

    base_points = [
        f"{x_of(row['date']):.1f},{y_of(float(row['close'])):.1f}"
        for row in daily.iter_rows(named=True)
    ]
    out.append(
        f'<polyline points="{" ".join(base_points)}" fill="none" '
        f'stroke="{COLORS["base"]}" stroke-width="1.5" opacity="0.72"/>'
    )

    pivots: list[tuple[date, float]] = [
        (rows[0]["start"], float(rows[0]["index_start"]))
    ]
    pivots.extend(
        (row["end"], float(row["index_end"]))
        for row in rows
    )
    for row in rows:
        start_x = x_of(row["start"])
        end_x = x_of(row["end"])
        start_y = y_of(float(row["index_start"]))
        end_y = y_of(float(row["index_end"]))
        ongoing = row["status"] == "ongoing"
        color = COLORS["up"] if row["direction"] == "up" else COLORS["down"]
        dash = ' stroke-dasharray="12 8"' if ongoing else ""
        out.append(
            f'<line x1="{start_x:.1f}" y1="{start_y:.1f}" '
            f'x2="{end_x:.1f}" y2="{end_y:.1f}" stroke="{color}" '
            f'stroke-width="4.5" stroke-linecap="round"{dash}/>'
        )
        middle_x = (start_x + end_x) / 2
        middle_y = (start_y + end_y) / 2
        width = end_x - start_x
        move = float(row["index_return"]) * 100
        calendar_days = (row["end"] - row["start"]).days
        label = (
            f"{row['wave_id']:02d}  {move:+.1f}% · "
            f"{calendar_days} days"
        )
        if width >= 145:
            label_width = max(132, len(label) * 9.5)
            label_y = middle_y - 22 if move > 0 else middle_y + 34
            out.append(
                f'<rect x="{middle_x-label_width/2:.1f}" '
                f'y="{label_y-22:.1f}" width="{label_width:.1f}" height="30" '
                f'rx="6" fill="{COLORS["bg"]}" stroke="{color}"/>'
            )
            out.append(
                _text(
                    middle_x,
                    label_y,
                    label,
                    size=15,
                    color=color,
                    anchor="middle",
                    weight=700,
                )
            )
        else:
            out.append(
                f'<circle cx="{middle_x:.1f}" cy="{middle_y:.1f}" r="14" '
                f'fill="{COLORS["bg"]}" stroke="{color}" stroke-width="2"/>'
            )
            out.append(
                _text(
                    middle_x,
                    middle_y + 5,
                    f"{row['wave_id']:02d}",
                    size=13,
                    color=color,
                    anchor="middle",
                    weight=700,
                )
            )

    for index, (pivot_date, price) in enumerate(pivots):
        x = x_of(pivot_date)
        y = y_of(price)
        color = COLORS["up"] if index == len(pivots) - 1 else COLORS["text"]
        out.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5.5" '
            f'fill="{COLORS["bg"]}" stroke="{color}" stroke-width="2.5"/>'
        )

    out.extend(
        [
            _text(
                LEFT,
                TABLE_TOP - 24,
                "SWING MEASUREMENT LEDGER",
                size=19,
                weight=700,
            ),
            _text(
                RIGHT,
                TABLE_TOP - 24,
                "Duration = natural calendar days",
                size=15,
                color=COLORS["muted"],
                anchor="end",
            ),
        ]
    )

    panel_gap = 28
    panel_width = (RIGHT - LEFT - panel_gap) / 2
    row_height = 48
    header_height = 42
    for panel_index in range(2):
        panel_x = LEFT + panel_index * (panel_width + panel_gap)
        panel_rows = rows[panel_index * 8:(panel_index + 1) * 8]
        panel_height = header_height + len(panel_rows) * row_height
        out.append(
            f'<rect x="{panel_x:.1f}" y="{TABLE_TOP}" '
            f'width="{panel_width:.1f}" height="{panel_height}" rx="8" '
            f'fill="{COLORS["panel"]}" stroke="{COLORS["border"]}"/>'
        )
        columns = [
            (18, "#", "start"),
            (60, "Direction", "start"),
            (175, "Start", "start"),
            (340, "End", "start"),
            (505, "Move", "end"),
            (680, "Duration", "end"),
        ]
        for offset, title, anchor in columns:
            out.append(
                _text(
                    panel_x + offset,
                    TABLE_TOP + 27,
                    title,
                    size=14,
                    color=COLORS["muted"],
                    anchor=anchor,
                    weight=700,
                )
            )
        out.append(
            f'<line x1="{panel_x}" y1="{TABLE_TOP+header_height}" '
            f'x2="{panel_x+panel_width}" y2="{TABLE_TOP+header_height}" '
            f'stroke="{COLORS["border"]}"/>'
        )
        for row_index, row in enumerate(panel_rows):
            y0 = TABLE_TOP + header_height + row_index * row_height
            if row_index % 2:
                out.append(
                    f'<rect x="{panel_x+1:.1f}" y="{y0:.1f}" '
                    f'width="{panel_width-2:.1f}" height="{row_height}" '
                    f'fill="{COLORS["panel_alt"]}"/>'
                )
            if row_index:
                out.append(
                    f'<line x1="{panel_x}" y1="{y0:.1f}" '
                    f'x2="{panel_x+panel_width}" y2="{y0:.1f}" '
                    f'stroke="{COLORS["grid"]}"/>'
                )
            center_y = y0 + 30
            color = (
                COLORS["up"]
                if row["direction"] == "up"
                else COLORS["down"]
            )
            calendar_days = (row["end"] - row["start"]).days
            direction = (
                "ONGOING"
                if row["status"] == "ongoing"
                else "UP" if row["direction"] == "up" else "DOWN"
            )
            values = [
                (18, f"{row['wave_id']:02d}", "start", COLORS["text"]),
                (60, direction, "start", color),
                (175, row["start"].isoformat(), "start", COLORS["text"]),
                (340, row["end"].isoformat(), "start", COLORS["text"]),
                (
                    505,
                    f"{float(row['index_return'])*100:+.1f}%",
                    "end",
                    color,
                ),
                (680, f"{calendar_days} days", "end", COLORS["text"]),
            ]
            for offset, value, anchor, value_color in values:
                out.append(
                    _text(
                        panel_x + offset,
                        center_y,
                        value,
                        size=15,
                        color=value_color,
                        anchor=anchor,
                        weight=700 if offset in {60, 505} else 400,
                        family="Menlo, Consolas, monospace",
                    )
                )

    footer_y = 1290
    out.extend(
        [
            _text(
                LEFT,
                footer_y,
                "Method: close-price ZigZag; a pivot is confirmed only after a "
                "12% reversal.",
                size=15,
                color=COLORS["muted"],
            ),
            _text(
                RIGHT,
                footer_y,
                "The red dashed final leg is provisional and may repaint.",
                size=15,
                color=COLORS["up"],
                anchor="end",
                weight=700,
            ),
            "</svg>",
        ]
    )
    OUTPUT.write_text("\n".join(out), encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(render())
