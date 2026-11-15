"""Replay historical one-minute bars through the live paper engine.

This is the bridge between a normal backtest and the forward runner: it uses the
same signal, bracket, fill, cost and flat-time code as ``scripts.run_lse_runner``
without needing an LSE connection.  Use it to validate a config before risking a
paper session, or to test a data file from another vendor.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import pandas as pd

from config.settings import NY_TZ
from src.indicators import normalise_bars, read_bars
from src.paper_session import PaperSession

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("skyq.range.paper_replay")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="runner JSON config")
    parser.add_argument("--data", required=True, help="one-minute parquet or CSV")
    parser.add_argument("--start", default=None, help="inclusive YYYY-MM-DD or timestamp")
    parser.add_argument("--end", default=None, help="inclusive date or timestamp")
    parser.add_argument("--warmup-bars", type=int, default=500)
    parser.add_argument("--output-dir", default=str(ROOT / "reports" / "paper_replay"))
    parser.add_argument("--max-bars", type=int, default=0, help="optional safety cap")
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    args = _parser().parse_args(argv)
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    session = PaperSession.from_config(config)
    raw = normalise_bars(read_bars(args.data))
    if args.start:
        raw = raw.loc[pd.Timestamp(args.start, tz=NY_TZ) :]
    if args.end:
        end = pd.Timestamp(args.end, tz=NY_TZ)
        if end.time() == pd.Timestamp("00:00").time():
            end = end + pd.Timedelta(days=1) - pd.Timedelta(minutes=1)
        raw = raw.loc[:end]
    clock = raw.index.time
    raw = raw[
        (clock >= pd.Timestamp("09:30").time())
        & (clock < pd.Timestamp("16:00").time())
    ]
    if args.max_bars:
        raw = raw.head(max(1, args.max_bars))
    if len(raw) <= args.warmup_bars:
        log.error("need more than --warmup-bars=%d; got %d", args.warmup_bars, len(raw))
        return 2
    session.seed(raw.iloc[: args.warmup_bars])
    session.sync(raw)
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    runner_id = session.settings.runner_id
    journal = output / f"{runner_id}.csv"
    session.journal_frame().to_csv(journal, index=False)
    summary = {
        "runner": session.settings.runner_id,
        "symbol": session.settings.symbol,
        "quantity": session.settings.quantity,
        "data": str(Path(args.data).resolve()),
        "start": str(raw.index.min()),
        "end": str(raw.index.max()),
        "bars": int(len(raw)),
        "journal": str(journal),
        **session.snapshot(),
    }
    (output / f"{runner_id}.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
