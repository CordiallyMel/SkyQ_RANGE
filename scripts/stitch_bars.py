"""Stitch multiple raw bar files into one deduplicated parquet.

Use this when a vendor delivered a series in date chunks (the LSE downloads in
this project arrive that way).  It accepts CSV or parquet, normalises timestamps
to New York, drops duplicate timestamps (last file wins), validates OHLC, and
writes a single parquet with no feature columns.

Example::

    python -m scripts.stitch_bars \
      --inputs /data/SPY_*.csv \
      --output data/raw/SPY_1m_stitched.parquet \
      --symbol SPY
"""

from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

import pandas as pd

from src.indicators import normalise_bars, read_bars


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--symbol", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths: list[Path] = []
    for pattern in args.inputs:
        matches = [Path(item) for item in glob.glob(pattern)]
        if matches:
            paths.extend(matches)
        else:
            paths.append(Path(pattern))
    paths = [path for path in paths if path.is_file()]
    if not paths:
        print("no input files matched", file=sys.stderr)
        return 2
    frames = []
    for path in paths:
        print(f"reading {path} ...", flush=True)
        frame = normalise_bars(read_bars(path))
        if frame.empty:
            print(f"  skipped empty {path}", file=sys.stderr)
            continue
        frames.append(frame)
    if not frames:
        print("all inputs were empty", file=sys.stderr)
        return 2
    combined = pd.concat(frames).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    bad = (
        (combined["high"] < combined[["open", "close", "low"]].max(axis=1))
        | (combined["low"] > combined[["open", "close", "high"]].min(axis=1))
        | (combined[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    if bad.any():
        print(f"dropping {int(bad.sum())} invalid OHLC rows", file=sys.stderr)
        combined = combined[~bad]
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    combined.to_parquet(output)
    print(
        f"wrote {output} | rows={len(combined):,} | "
        f"{combined.index.min()} -> {combined.index.max()} | "
        f"symbol={args.symbol or 'from file'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
