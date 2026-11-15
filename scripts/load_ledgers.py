"""Load full canonical RANGE trade ledgers into PostgreSQL.

The committed repository carries only a recent-trade sample.  This command
loads every row from the canonical ledgers so SQL can reproduce the full
research record.

Example:
    DATABASE_URL=postgresql+psycopg://... python -m scripts.load_ledgers \
        --spy /path/to/skyq_range_spy_1000sh_2003_2026_trades.csv \
        --qqq /path/to/skyq_range_qqq_1000sh_2003_2026_trades.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from src.database import init_db, make_engine, session_factory
from src.models import AuditEvent, BacktestRun, Trade


def _dt(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, utc=True, errors="coerce")
    return parsed


def _load(session, path: Path, symbol: str) -> int:
    run = session.scalar(
        select(BacktestRun).where(
            BacktestRun.symbol == symbol,
            BacktestRun.window == "2003-2026",
            BacktestRun.strategy == "SkyQ RANGE",
        )
    )
    if run is None:
        raise SystemExit("seed the runs first with scripts.init_db")
    frame = pd.read_csv(path)
    frame["entry_time"] = _dt(frame["entry_time"])
    frame["exit_time"] = _dt(frame["exit_time"])
    frame = frame.dropna(subset=["entry_time", "exit_time"])
    session.query(Trade).filter(Trade.run_id == run.id).delete(synchronize_session=False)
    rows = [
        Trade(
            run_id=run.id,
            symbol=symbol,
            side=str(row.get("side", "LONG")).upper(),
            bar_time=pd.Timestamp(row["bar_time"]).to_pydatetime()
            if pd.notna(row.get("bar_time"))
            else None,
            signal_time=pd.Timestamp(row["signal_time"]).to_pydatetime()
            if pd.notna(row.get("signal_time"))
            else None,
            entry_time=row["entry_time"].to_pydatetime(),
            entry_price=float(row.get("entry_price", 0)),
            stop_price=float(row.get("stop_price", 0)),
            target_price=float(row.get("target_price", 0)),
            exit_time=row["exit_time"].to_pydatetime(),
            exit_price=float(row.get("exit_price", 0)),
            exit_reason=str(row.get("exit_reason", "")),
            quantity=int(row.get("quantity", 0)),
            gross_pnl=float(row.get("gross_pnl", 0)),
            costs=float(row.get("costs", 0)),
            net_pnl=float(row.get("net_pnl", 0)),
            bars_held=int(row.get("bars_held", 0)),
            channel_width=float(row.get("channel_width", 0)),
            zone_price=float(row.get("zone_price", 0)),
        )
        for _, row in frame.iterrows()
    ]
    session.add_all(rows)
    session.add(
        AuditEvent(
            action="ledger.load",
            detail={"symbol": symbol, "rows": len(rows), "file": path.name},
        )
    )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spy", required=True)
    parser.add_argument("--qqq", required=True)
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    engine = make_engine(args.database_url)
    init_db(engine)
    factory = session_factory(engine)
    with factory() as session:
        spy = _load(session, Path(args.spy), "SPY")
        qqq = _load(session, Path(args.qqq), "QQQ")
        session.commit()
    print(f"loaded {spy:,} SPY and {qqq:,} QQQ trades into {engine.url.database or engine.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
