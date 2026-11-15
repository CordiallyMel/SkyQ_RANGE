#!/usr/bin/env python3
"""Generate the README charts from the committed published data.

This script does not need the large source parquet files, a database, or a
browser.  It reads ``data/published/equity_curve.csv`` and
``data/published/futures_gate.csv`` and writes two SVG assets under
``docs/assets/``.

    python -m scripts.make_equity_chart
"""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PUBLISHED = ROOT / "data" / "published"
OUT = ROOT / "docs" / "assets"

BG = "#070b14"
PANEL = "#0c1420"
GRID = "#1b2a3a"
TEXT = "#e7eef8"
MUTED = "#7d8ea4"
MINT = "#5eead4"
BLUE = "#60a5fa"
ROSE = "#fb7185"
AMBER = "#fbbf24"
FONT = "Inter, Segoe UI, Helvetica, Arial, sans-serif"
MONO = "SFMono-Regular, Cascadia Code, Consolas, monospace"


def money(value: float) -> str:
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def _downsample(points: list[tuple[pd.Timestamp, float]], limit: int = 900):
    if len(points) <= limit:
        return points
    step = max(1, len(points) // limit)
    sampled = points[::step]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])
    return sampled


def equity_svg() -> str:
    frame = pd.read_csv(PUBLISHED / "equity_curve.csv")
    frame["date"] = pd.to_datetime(frame["date"], utc=True)
    frame = frame[frame["window"] == "2003-2026"]
    series = {
        "SPY": frame[frame["symbol"] == "SPY"].sort_values("date"),
        "QQQ": frame[frame["symbol"] == "QQQ"].sort_values("date"),
    }
    if any(part.empty for part in series.values()):
        raise SystemExit("published equity curve is missing SPY or QQQ")

    width, height = 1200, 520
    left, right, top, bottom = 84, 28, 66, 52
    plot_w, plot_h = width - left - right, height - top - bottom
    all_values = [float(value) for part in series.values() for value in part["equity"]]
    lo = min(all_values + [1_000_000.0]) * 0.94
    hi = max(all_values) * 1.04
    start = min(part["date"].iloc[0] for part in series.values())
    end = max(part["date"].iloc[-1] for part in series.values())

    def x(stamp: pd.Timestamp) -> float:
        total = (end - start).total_seconds() or 1
        return left + (stamp - start).total_seconds() / total * plot_w

    def y(value: float) -> float:
        return top + (hi - value) / (hi - lo) * plot_h

    paths: list[str] = []
    for symbol, part in series.items():
        points = _downsample(
            [(pd.Timestamp(row.date), float(row.equity)) for row in part.itertuples()]
        )
        colour = MINT if symbol == "SPY" else BLUE
        line = "M " + " L ".join(f"{x(d):.1f},{y(v):.1f}" for d, v in points)
        area = (
            f"{line} L {x(points[-1][0]):.1f},{top + plot_h:.1f} "
            f"L {x(points[0][0]):.1f},{top + plot_h:.1f} Z"
        )
        paths.append(
            f'<path d="{area}" fill="url(#fill{symbol})"/>'
            f'<path d="{line}" fill="none" stroke="{colour}" stroke-width="2.2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{x(points[-1][0]):.1f}" cy="{y(points[-1][1]):.1f}" r="4" '
            f'fill="{colour}" stroke="{BG}" stroke-width="2"/>'
        )

    ticks = [lo + (hi - lo) * index / 4 for index in range(5)]
    grid = "".join(
        f'<line x1="{left}" y1="{y(value):.1f}" x2="{width - right}" y2="{y(value):.1f}" '
        f'stroke="{GRID}" stroke-dasharray="3 6"/>'
        f'<text x="{left - 12}" y="{y(value) + 4:.1f}" text-anchor="end" fill="{MUTED}" '
        f'font-family="{MONO}" font-size="10">{money(value)}</text>'
        for value in ticks
    )
    year_ticks = [year for year in (2003, 2007, 2011, 2015, 2019, 2023, 2026)]
    xlabels = "".join(
        f'<text x="{x(pd.Timestamp(f"{year}-01-01", tz="UTC")):.1f}" y="{height - 20}" '
        f'text-anchor="middle" fill="{MUTED}" font-family="{MONO}" font-size="10">{year}</text>'
        for year in year_ticks
    )
    spy = series["SPY"]["equity"].iloc[-1]
    qqq = series["QQQ"]["equity"].iloc[-1]
    annotations = (
        f'<text x="{width - right}" y="30" text-anchor="end" fill="{MUTED}" '
        f'font-family="{MONO}" font-size="9" letter-spacing="1">'
        f"FIXED 1,000 SHARES / $1M START / NOT COMPOUNDED</text>"
        f'<g transform="translate({left}, 43)">'
        f'<line x1="0" y1="0" x2="18" y2="0" stroke="{MINT}" stroke-width="2"/>'
        f'<text x="25" y="4" fill="{TEXT}" font-family="{MONO}" font-size="10">SPY {money(spy)}</text>'
        f'<line x1="130" y1="0" x2="148" y2="0" stroke="{BLUE}" stroke-width="2"/>'
        f'<text x="155" y="4" fill="{TEXT}" font-family="{MONO}" font-size="10">QQQ {money(qqq)}</text>'
        f"</g>"
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-labelledby="title desc">
<title id="title">SkyQ RANGE fixed-size equity curves for SPY and QQQ</title>
<desc id="desc">Closed-trade equity from a $1,000,000 starting account with a fixed 1,000 shares and no compounding, after modeled commissions and slippage. SPY ends at {spy:,.0f}; QQQ ends at {qqq:,.0f}.</desc>
<rect width="{width}" height="{height}" rx="14" fill="{BG}"/>
<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" fill="none" stroke="#223246"/>
<defs>
  <linearGradient id="fillSPY" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="{MINT}" stop-opacity=".22"/><stop offset="100%" stop-color="{MINT}" stop-opacity="0"/></linearGradient>
  <linearGradient id="fillQQQ" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="{BLUE}" stop-opacity=".15"/><stop offset="100%" stop-color="{BLUE}" stop-opacity="0"/></linearGradient>
</defs>
<text x="{left}" y="31" fill="{TEXT}" font-family="{FONT}" font-size="17" font-weight="600">SkyQ RANGE / two ETF books</text>
{annotations}
{grid}
<line x1="{left}" y1="{y(1_000_000):.1f}" x2="{width - right}" y2="{y(1_000_000):.1f}" stroke="{AMBER}" stroke-opacity=".5" stroke-dasharray="5 5"/>
<text x="{width - right}" y="{y(1_000_000) - 7:.1f}" text-anchor="end" fill="{AMBER}" font-family="{MONO}" font-size="9">$1M bankroll</text>
{''.join(paths)}
{xlabels}
</svg>
'''


def gate_svg() -> str:
    frame = pd.read_csv(PUBLISHED / "futures_gate.csv").sort_values("net_per_month")
    width = 1200
    row_height = 40
    height = 110 + row_height * len(frame)
    left, right, top = 260, 60, 82
    plot_w = width - left - right
    max_abs = max(1.0, frame["net_per_month"].abs().max())
    rows = []
    for index, row in enumerate(frame.itertuples()):
        y = top + index * row_height
        bar = max(2.0, abs(float(row.net_per_month)) / max_abs * plot_w)
        label = f"{row.symbol}  {row.description}"
        rows.append(
            f'<text x="{left - 14}" y="{y + 15}" text-anchor="end" fill="{TEXT}" '
            f'font-family="{MONO}" font-size="10">{html.escape(label)}</text>'
            f'<rect x="{left}" y="{y + 3}" width="{bar:.1f}" height="17" rx="3" '
            f'fill="{ROSE}" fill-opacity=".38" stroke="{ROSE}" stroke-opacity=".65"/>'
            f'<text x="{left + bar + 8}" y="{y + 16}" fill="{ROSE}" font-family="{MONO}" '
            f'font-size="10">{row.net_per_month:+,.0f}/mo</text>'
        )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-labelledby="title desc">
<title id="title">RANGE futures cross-asset gate</title>
<desc id="desc">Every tested futures series loses money after modeled costs. Values are monthly net P&amp;L.</desc>
<rect width="{width}" height="{height}" rx="14" fill="{BG}"/>
<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="14" fill="none" stroke="#3a2230"/>
<text x="{left}" y="34" fill="{TEXT}" font-family="{FONT}" font-size="17" font-weight="600">The same geometry, on futures</text>
<text x="{left}" y="54" fill="{MUTED}" font-family="{MONO}" font-size="10">NET PER MONTH / 1 CONTRACT / AFTER COMMISSIONS AND SLIPPAGE</text>
{"".join(rows)}
</svg>
'''


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "equity_curve.svg").write_text(equity_svg(), encoding="utf-8")
    (OUT / "futures_gate.svg").write_text(gate_svg(), encoding="utf-8")
    print(f"wrote {OUT / 'equity_curve.svg'}")
    print(f"wrote {OUT / 'futures_gate.svg'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
