"""Load user-supplied one-minute bars into SQLite or PostgreSQL.

The repository's reporting schema is small and portable.  Research archives are
not: a 24-year, four-million-bar file is better kept in a market-data table than
pushed into the trade read model.  This loader writes to ``market_bars`` and
works with parquet or CSV.

Examples::

    # local, no server required
    python -m scripts.import_market_data \
      --file /data/SPY_1m_24y.parquet --symbol SPY --dataset etf-24y \
      --sqlite data/market/range_market.sqlite --replace

    # PostgreSQL
    python -m scripts.import_market_data \
      --file /data/ym_1m.parquet --symbol YM --dataset futures-1m \
      --database-url "$DATABASE_URL"

For a first load use ``--replace``; it removes only that dataset+symbol's rows,
not the reporting trades.  The schema is in ``sql/003_market_bars.sql``.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

from src.indicators import normalise_bars, read_bars

ROOT = Path(__file__).resolve().parents[1]
TABLE = "market_bars"
COLUMNS = ["open", "high", "low", "close", "volume"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", required=True, help="parquet or CSV with timestamps")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--dataset", default="user-1m")
    parser.add_argument("--sqlite", default=None, help="SQLite output path")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--replace", action="store_true", help="replace dataset+symbol rows")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--chunksize", type=int, default=50_000)
    return parser


def _prepare(args: argparse.Namespace) -> pd.DataFrame:
    frame = normalise_bars(read_bars(args.file))
    if frame.empty:
        raise SystemExit(f"no OHLCV rows found in {args.file}")
    if args.start:
        frame = frame.loc[pd.Timestamp(args.start, tz=frame.index.tz) :]
    if args.end:
        frame = frame.loc[: pd.Timestamp(args.end, tz=frame.index.tz)]
    if args.limit:
        frame = frame.head(max(1, args.limit))
    payload = frame.reset_index()
    payload.columns = [str(column).lower() for column in payload.columns]
    timestamp_column = next(
        (name for name in ("time", "timestamp", "date", "datetime") if name in payload),
        None,
    )
    if timestamp_column is None:
        raise SystemExit("the data has no timestamp/index column")
    payload = payload.rename(columns={timestamp_column: "ts"})
    payload["ts"] = pd.to_datetime(payload["ts"], utc=True)
    payload["symbol"] = args.symbol.upper()
    payload["dataset"] = args.dataset
    payload["source"] = str(Path(args.file).resolve())
    for column in COLUMNS:
        if column not in payload:
            raise SystemExit(f"missing {column} column")
        payload[column] = pd.to_numeric(payload[column], errors="coerce")
    payload = payload.dropna(subset=["ts", *COLUMNS])
    return payload[
        ["dataset", "symbol", "ts", *COLUMNS, "source"]
    ].sort_values("ts")


def _sqlite_load(path: Path, frame: pd.DataFrame, replace: bool, chunksize: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS market_bars (
                dataset TEXT NOT NULL,
                symbol TEXT NOT NULL,
                ts TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (dataset, symbol, ts)
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_bars_symbol_ts "
            "ON market_bars (symbol, ts)"
        )
        if replace:
            dataset = str(frame["dataset"].iloc[0])
            symbol = str(frame["symbol"].iloc[0])
            connection.execute(
                "DELETE FROM market_bars WHERE dataset = ? AND symbol = ?",
                (dataset, symbol),
            )
        frame = frame.copy()
        frame["ts"] = frame["ts"].dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        frame.to_sql(TABLE, connection, if_exists="append", index=False, chunksize=chunksize)
        connection.commit()


def _postgres_load(url: str, frame: pd.DataFrame, replace: bool, chunksize: int) -> None:
    raw_schema = (ROOT / "sql" / "003_market_bars.sql").read_text(encoding="utf-8")
    # SQLAlchemy already owns the transaction; strip psql's wrapper/comments.
    schema = "\n".join(
        line
        for line in raw_schema.splitlines()
        if line.strip()
        and not line.strip().startswith("--")
        and line.strip().upper() not in {"BEGIN;", "COMMIT;"}
    )
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(schema))
        if replace:
            connection.execute(
                text(
                    "DELETE FROM market_bars WHERE dataset = :dataset AND symbol = :symbol"
                ),
                {
                    "dataset": str(frame["dataset"].iloc[0]),
                    "symbol": str(frame["symbol"].iloc[0]),
                },
            )
    frame.to_sql(TABLE, engine, if_exists="append", index=False, chunksize=chunksize)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.sqlite and not args.database_url:
        print("pass --sqlite PATH or --database-url URL", file=sys.stderr)
        return 2
    frame = _prepare(args)
    print(
        f"{len(frame):,} rows | {frame['symbol'].iloc[0]} | "
        f"{frame['ts'].min()} -> {frame['ts'].max()}",
        flush=True,
    )
    if args.sqlite:
        _sqlite_load(Path(args.sqlite).resolve(), frame, args.replace, args.chunksize)
        print(f"wrote SQLite {Path(args.sqlite).resolve()}", flush=True)
    if args.database_url:
        _postgres_load(args.database_url, frame, args.replace, args.chunksize)
        print("wrote PostgreSQL market_bars", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
