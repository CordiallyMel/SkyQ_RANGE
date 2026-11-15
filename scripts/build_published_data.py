"""Build the dashboard's small published-data bundle.

Full trade ledgers are intentionally not committed: the two RANGE books contain
more than 100,000 rows.  This script derives the daily equity curves and a small
recent-trade sample from those ledgers, and filters the family comparison CSV to
the RANGE rows.  The full ledgers can still be loaded into PostgreSQL with
``scripts/load_ledgers.py``.

Usage (from the repository root, with the source ledgers available):

    python -m scripts.build_published_data \
        --ledger-dir /path/to/ledgers \
        --comparison /path/to/etf_1000sh_comparison.csv
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "published"
WINDOWS = {
    "2003-2026": ("2003-09-10", "2026-11-04"),
    "2007-2026": ("2007-01-03", "2026-11-04"),
}
RECENT_ROWS = 250


def _find_ledger(ledger_dirs: list[Path], symbol: str) -> Path:
    suffix = f"skyq_range_{symbol.lower()}_1000sh_2003_2026_trades.csv"
    for directory in ledger_dirs:
        path = directory / suffix
        if path.exists():
            return path
    raise FileNotFoundError(
        f"could not find {suffix} in {', '.join(str(path) for path in ledger_dirs)}"
    )


def _equity(ledger: pd.DataFrame, start: str) -> pd.DataFrame:
    data = ledger.copy()
    data["exit_time"] = pd.to_datetime(data["exit_time"], utc=True, errors="coerce")
    data = data[data["exit_time"].notna()].sort_values("exit_time")
    data = data[data["exit_time"] >= pd.Timestamp(start, tz="UTC")]
    if data.empty:
        return pd.DataFrame(columns=["date", "daily_pnl", "equity", "drawdown"])
    data["date"] = data["exit_time"].dt.date
    daily = data.groupby("date", sort=True)["net_pnl"].sum().reset_index()
    daily["equity"] = 1_000_000.0 + daily["net_pnl"].cumsum()
    daily["peak"] = daily["equity"].cummax()
    daily["drawdown"] = daily["equity"] - daily["peak"]
    daily = daily.rename(columns={"net_pnl": "daily_pnl"})
    return daily[["date", "daily_pnl", "equity", "drawdown"]]


def _recent(ledger: pd.DataFrame, symbol: str) -> pd.DataFrame:
    data = ledger.copy()
    data["exit_time"] = pd.to_datetime(data["exit_time"], utc=True, errors="coerce")
    data = data[data["exit_time"].notna()].sort_values("exit_time").tail(RECENT_ROWS)
    columns = [
        "side",
        "bar_time",
        "signal_time",
        "entry_time",
        "entry_price",
        "stop_price",
        "target_price",
        "exit_time",
        "exit_price",
        "exit_reason",
        "quantity",
        "gross_pnl",
        "costs",
        "net_pnl",
        "bars_held",
        "channel_width",
        "zone_price",
    ]
    present = [column for column in columns if column in data.columns]
    result = data[present].copy()
    result.insert(0, "symbol", symbol)
    result.insert(0, "rank", range(len(result), 0, -1))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger-dir", action="append", default=[])
    parser.add_argument("--comparison", default=None)
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ledger_dirs = [Path(item) for item in args.ledger_dir] or [
        Path("data") / "ledgers",
        Path("logs"),
    ]

    comparison_candidates = [
        Path(args.comparison) if args.comparison else None,
        Path("data") / "etf_1000sh_comparison.csv",
        ROOT / "data" / "published" / "etf_1000sh_comparison.csv",
    ]
    comparison_path = next(
        (path for path in comparison_candidates if path is not None and path.exists()),
        None,
    )
    if comparison_path is None:
        raise FileNotFoundError("etf_1000sh_comparison.csv was not found")
    comparison = pd.read_csv(comparison_path)
    comparison = comparison[comparison["strategy"].str.contains("RANGE", case=False, na=False)]
    comparison.to_csv(out / "range_metrics.csv", index=False)

    equity_frames: list[pd.DataFrame] = []
    recent_frames: list[pd.DataFrame] = []
    provenance: dict[str, object] = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "source_comparison": str(comparison_path.name),
        "source_ledgers": {},
        "starting_equity": 1_000_000.0,
        "note": "Equity curves are closed-trade, fixed-size, and non-compounded.",
    }
    for symbol in ("SPY", "QQQ"):
        ledger_path = _find_ledger(ledger_dirs, symbol)
        ledger = pd.read_csv(ledger_path)
        digest = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
        provenance["source_ledgers"][symbol] = {
            "file": ledger_path.name,
            "rows": int(len(ledger)),
            "sha256": digest,
        }
        recent_frames.append(_recent(ledger, symbol))
        for window, (start, _end) in WINDOWS.items():
            curve = _equity(ledger, start)
            curve.insert(0, "window", window)
            curve.insert(1, "symbol", symbol)
            equity_frames.append(curve)

    pd.concat(equity_frames, ignore_index=True).to_csv(
        out / "equity_curve.csv", index=False
    )
    pd.concat(recent_frames, ignore_index=True).to_csv(
        out / "recent_trades.csv", index=False
    )
    (out / "provenance.json").write_text(
        pd.Series(provenance).to_json(force_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {len(comparison)} metric rows, {sum(len(f) for f in equity_frames):,} equity rows")
    print(f"wrote {sum(len(f) for f in recent_frames):,} recent trades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
