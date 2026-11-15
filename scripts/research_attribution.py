"""Full-history attribution and one-factor sensitivity for the 1,000-share books.

The canonical result is a fixed-size, closed-trade study.  This script does not
re-select parameters on a test set; it asks two narrower questions:

1. Where did the net P&L come from (side, exit, hour, year, channel width)?
2. How much does the result move when one deployment knob is changed?

Run with the full one-minute parquet files outside Git::

    python -m scripts.research_attribution \
      --data-root "/path/to/Data_For_Backtests" \
      --out reports

Outputs are safe to commit: CSV tables plus a readable Markdown note.  The
script deliberately uses the same ``run_backtest`` path as the published
results, so a run with the baseline settings reproduces the canonical metrics.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config.settings import BOOK_PRESETS, instrument
from src.backtest import BacktestResult, run_backtest
from src.indicators import load_bars


@dataclass(frozen=True)
class Book:
    symbol: str
    file: str
    start: str
    end: str


BOOKS = (
    Book("SPY", "SPY_1m_24y.parquet", "2003-09-10", "2026-11-04"),
    Book("QQQ", "QQQ_1m_24y.parquet", "2003-09-10", "2026-11-04"),
)


def _resolve(root: Path, filename: str) -> Path:
    for candidate in (root / filename, Path("data") / filename, Path("data") / "etf" / filename):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"missing {filename} under {root}")


def _preset(symbol: str) -> dict[str, float]:
    preset = BOOK_PRESETS[symbol]
    return {
        "stop_ticks": float(preset.stop_ticks),
        "max_hold_bars": int(preset.max_hold_bars),
        "min_channel_pct": float(preset.min_channel_pct),
    }


def _load(book: Book, root: Path) -> tuple[pd.DataFrame, dict[str, float]]:
    path = _resolve(root, book.file)
    print(f"loading {book.symbol} from {path} ...", flush=True)
    frame = load_bars(path, timeframe="1m", start=book.start, end=book.end)
    return frame, _preset(book.symbol)


def _run(
    book: Book,
    frame: pd.DataFrame,
    preset: dict[str, float],
    **overrides: float | int | str,
) -> BacktestResult:
    params: dict[str, float | int | str] = {
        "start": book.start,
        "end": book.end,
        "sides": "both",
        "quantity": 1000,
        "entry_zone": 0.20,
        "min_channel_pct": preset["min_channel_pct"],
        "min_channel_width": 0.0,
        "stop_ticks": preset["stop_ticks"],
        "tp_mode": "channel",
        "tp_channel_pct": 0.20,
        "cooldown_bars": 0,
        "overlap": True,
        "atr_filter": False,
        "session": "rth",
        "max_hold_bars": preset["max_hold_bars"],
        "flat_at_minute": 15 * 60 + 55,
        "timeframe": "1m",
        "assume_stop_first": True,
    }
    params.update(overrides)
    return run_backtest(frame, instrument(book.symbol), **params)


def _trade_frame(result: BacktestResult) -> pd.DataFrame:
    rows = []
    for trade in result.trades:
        rows.append(
            {
                "symbol": trade.symbol,
                "side": trade.side.value,
                "bar_time": pd.Timestamp(trade.bar_time),
                "entry_time": pd.Timestamp(trade.entry_time),
                "exit_time": pd.Timestamp(trade.exit_time),
                "exit_reason": trade.exit_reason,
                "net_pnl": trade.net_pnl,
                "gross_pnl": trade.gross_pnl,
                "costs": trade.costs,
                "channel_width": trade.channel_width,
                "bars_held": trade.bars_held,
                "is_win": trade.is_win,
            }
        )
    frame = pd.DataFrame(rows)
    if not frame.empty:
        local = frame["bar_time"].dt.tz_convert("America/New_York")
        frame["hour"] = local.dt.hour
        frame["weekday"] = local.dt.day_name()
        frame["year"] = local.dt.year
        frame["width_bucket"] = pd.qcut(
            frame["channel_width"],
            q=4,
            duplicates="drop",
        ).astype(str)
        frame["duration_bucket"] = pd.cut(
            frame["bars_held"],
            bins=[-1, 2, 5, 15, 60, 10**9],
            labels=["1-2", "3-5", "6-15", "16-60", "60+"],
        ).astype(str)
    return frame


def _aggregate(frame: pd.DataFrame, dimension: str) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame()
    grouped = frame.groupby(dimension, dropna=False)
    rows = []
    total_net = float(frame["net_pnl"].sum())
    total_trades = len(frame)
    for key, group in grouped:
        wins = int(group["is_win"].sum())
        gross_profit = float(group.loc[group["net_pnl"] > 0, "net_pnl"].sum())
        gross_loss = abs(float(group.loc[group["net_pnl"] < 0, "net_pnl"].sum()))
        net = float(group["net_pnl"].sum())
        rows.append(
            {
                "bucket": str(key),
                "trades": int(len(group)),
                "trade_share": len(group) / total_trades if total_trades else 0.0,
                "win_rate": wins / len(group) * 100.0 if len(group) else 0.0,
                "pf": gross_profit / gross_loss if gross_loss > 0 else math.nan,
                "net": net,
                "avg_net": net / len(group) if len(group) else 0.0,
                "costs": float(group["costs"].sum()),
                "net_share": net / total_net if total_net else math.nan,
            }
        )
    return pd.DataFrame(rows).sort_values("net", ascending=False)


def _markdown(
    book: Book,
    baseline: BacktestResult,
    attribution: dict[str, pd.DataFrame],
    sensitivity: pd.DataFrame,
    generated: str,
) -> str:
    summary = baseline.summary()
    lines = [
        f"# {book.symbol} attribution and sensitivity",
        "",
        f"Generated on {generated} from `{book.file}`.",
        "",
        "## Baseline",
        "",
        f"- Trades: **{int(summary['trades']):,}**",
        f"- Net: **${float(summary['net']):,.2f}**",
        f"- PF: **{float(summary['pf']):.3f}**",
        f"- Win rate: **{float(summary['win_rate']) * 100:.2f}%**",
        f"- Max drawdown: **-${float(summary['max_drawdown']):,.2f}**",
        "",
        "The baseline uses both sides, 1,000 shares, 20% channel entry, 20% "
        "channel target, the symbol preset stop/time stop, regular hours and "
        "15:55 ET flat.",
        "",
        "## Attribution",
        "",
        "These are descriptive decompositions of the canonical trade ledger, not "
        "independent tests. A bucket is a place the P&L came from, not a new "
        "entry rule.",
        "",
    ]
    for dimension, table in attribution.items():
        if table.empty:
            continue
        lines += [
            f"### {dimension.replace('_', ' ').title()}",
            "",
            "| Bucket | Trades | Share | Win | PF | Net | Net/share | Avg/trade |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for row in table.itertuples(index=False):
            pf = "—" if pd.isna(row.pf) else f"{row.pf:.3f}"
            share = "—" if pd.isna(row.net_share) else f"{row.net_share * 100:.1f}%"
            lines.append(
                f"| {row.bucket} | {row.trades:,} | {row.trade_share * 100:.1f}% | "
                f"{row.win_rate:.2f}% | {pf} | {row.net:+,.2f} | {share} | "
                f"{row.avg_net:+,.2f} |"
            )
        lines.append("")
    lines += [
        "## One-factor sensitivity",
        "",
        "Each row changes one knob from the baseline and holds the rest fixed. "
        "This is a stability check, not an optimisation exercise.",
        "",
        "| Parameter | Value | Trades | Net | Delta net | PF | Win | Max DD |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sensitivity.itertuples(index=False):
        pf = "—" if pd.isna(row.profit_factor) else f"{row.profit_factor:.3f}"
        lines.append(
            f"| {row.parameter} | {row.value} | {row.trades:,} | "
            f"{row.net:+,.2f} | {row.delta_net:+,.2f} | {pf} | "
            f"{row.win_rate * 100:.2f}% | {row.max_drawdown:,.2f} |"
        )
    lines += [
        "",
        "## Interpretation limits",
        "",
        "- Closed-trade, fixed-size P&L; no compounding or intra-trade mark-to-market.",
        "- Selected on the same history; sensitivity is not out-of-sample proof.",
        "- The stop is assumed first when a bar touches both stop and target.",
        "- A bucket with few trades can look strong by chance; check the trade count.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data", help="directory with the 24y parquet files")
    parser.add_argument("--out", default="reports")
    parser.add_argument("--symbols", default="SPY,QQQ")
    args = parser.parse_args(argv)
    wanted = {part.strip().upper() for part in args.symbols.split(",") if part.strip()}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    all_attribution: list[pd.DataFrame] = []
    all_sensitivity: list[pd.DataFrame] = []
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for book in BOOKS:
        if book.symbol not in wanted:
            continue
        frame, preset = _load(book, Path(args.data_root))
        baseline = _run(book, frame, preset)
        summary = baseline.summary()
        print(
            f"{book.symbol} baseline trades={int(summary['trades']):,} "
            f"net={float(summary['net']):,.2f} pf={float(summary['pf']):.3f}",
            flush=True,
        )
        trades = _trade_frame(baseline)
        attribution: dict[str, pd.DataFrame] = {}
        for dimension in (
            "side",
            "exit_reason",
            "hour",
            "weekday",
            "year",
            "width_bucket",
            "duration_bucket",
        ):
            table = _aggregate(trades, dimension)
            if table.empty:
                continue
            table.insert(0, "dimension", dimension)
            table.insert(0, "symbol", book.symbol)
            attribution[dimension] = table
            all_attribution.append(table)

        variants: list[tuple[str, float | int | str, dict[str, object]]] = [
            ("stop_ticks", preset["stop_ticks"] * 0.5, {"stop_ticks": preset["stop_ticks"] * 0.5}),
            ("stop_ticks", preset["stop_ticks"], {"stop_ticks": preset["stop_ticks"]}),
            ("stop_ticks", preset["stop_ticks"] * 1.5, {"stop_ticks": preset["stop_ticks"] * 1.5}),
            ("tp_channel_pct", 0.10, {"tp_channel_pct": 0.10}),
            ("tp_channel_pct", 0.20, {"tp_channel_pct": 0.20}),
            ("tp_channel_pct", 0.30, {"tp_channel_pct": 0.30}),
            ("entry_zone", 0.10, {"entry_zone": 0.10}),
            ("entry_zone", 0.20, {"entry_zone": 0.20}),
            ("entry_zone", 0.30, {"entry_zone": 0.30}),
            (
                "max_hold_bars",
                max(30, int(preset["max_hold_bars"]) // 2),
                {"max_hold_bars": max(30, int(preset["max_hold_bars"]) // 2)},
            ),
            (
                "max_hold_bars",
                int(preset["max_hold_bars"]),
                {"max_hold_bars": int(preset["max_hold_bars"])},
            ),
            (
                "max_hold_bars",
                min(480, int(preset["max_hold_bars"]) * 2),
                {"max_hold_bars": min(480, int(preset["max_hold_bars"]) * 2)},
            ),
            ("cooldown_bars", 0, {"cooldown_bars": 0}),
            ("cooldown_bars", 4, {"cooldown_bars": 4}),
            ("cooldown_bars", 8, {"cooldown_bars": 8}),
            ("sides", "both", {"sides": "both"}),
            ("sides", "long", {"sides": "long"}),
            ("sides", "short", {"sides": "short"}),
        ]
        sensitivity_rows: list[dict[str, object]] = []
        base_net = float(summary["net"])
        for parameter, value, overrides in variants:
            result = _run(book, frame, preset, **overrides)  # type: ignore[arg-type]
            s = result.summary()
            sensitivity_rows.append(
                {
                    "symbol": book.symbol,
                    "parameter": parameter,
                    "value": value,
                    "trades": int(s["trades"]),
                    "net": float(s["net"]),
                    "delta_net": float(s["net"]) - base_net,
                    "profit_factor": float(s["pf"]) if pd.notna(s["pf"]) else math.nan,
                    "win_rate": float(s["win_rate"]),
                    "max_drawdown": float(s["max_drawdown"]),
                }
            )
            print(
                f"  {parameter}={value}: net={float(s['net']):,.2f} "
                f"trades={int(s['trades']):,}",
                flush=True,
            )
        sensitivity = pd.DataFrame(sensitivity_rows)
        all_sensitivity.append(sensitivity)
        (out / f"attribution_{book.symbol}.md").write_text(
            _markdown(book, baseline, attribution, sensitivity, generated),
            encoding="utf-8",
        )
        for dimension, table in attribution.items():
            table.to_csv(out / f"attribution_{book.symbol}_{dimension}.csv", index=False)

    if all_attribution:
        pd.concat(all_attribution, ignore_index=True).to_csv(
            out / "attribution.csv", index=False
        )
    if all_sensitivity:
        pd.concat(all_sensitivity, ignore_index=True).to_csv(
            out / "sensitivity.csv", index=False
        )
        combined = "\n\n".join(
            (out / f"attribution_{symbol}.md").read_text(encoding="utf-8")
            for symbol in sorted(wanted)
            if (out / f"attribution_{symbol}.md").is_file()
        )
        (out / "ATTRIBUTION_AND_SENSITIVITY.md").write_text(combined, encoding="utf-8")
    print(f"wrote attribution and sensitivity tables under {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
