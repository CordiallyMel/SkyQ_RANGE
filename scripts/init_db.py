"""Initialize and seed the SkyQ RANGE reporting database.

The default target is ``DATABASE_URL``.  If no PostgreSQL URL is supplied, the
script uses a local SQLite database under ``var/`` so the dashboard can run
without a server.  The production path is PostgreSQL; ``sql/001_schema.sql``
contains the equivalent DDL for managed deployments.

Usage:
    python -m scripts.init_db
    DATABASE_URL=postgresql+psycopg://... python -m scripts.init_db
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.settings import BOOK_PRESETS, INSTRUMENTS
from src.app_config import ROOT, mask_database_url
from src.database import init_db, make_engine, session_factory
from src.models import (
    AuditEvent,
    BacktestMetric,
    BacktestRun,
    EquityPoint,
    FuturesGate,
    Instrument,
    Trade,
)

PUBLISHED = ROOT / "data" / "published"


def _dt(value) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or pd.isna(value):
        return None
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC").to_pydatetime()


def _seed_instruments(session: Session) -> None:
    for symbol, profile in INSTRUMENTS.items():
        row = session.get(Instrument, symbol)
        live = symbol in BOOK_PRESETS
        if row is None:
            session.add(
                Instrument(
                    symbol=symbol,
                    name=profile.description,
                    asset_class=profile.asset_class,
                    exchange=profile.exchange,
                    tick_size=profile.tick_size,
                    multiplier=profile.multiplier,
                    live_enabled=live,
                    note=(
                        "Live RANGE book"
                        if live
                        else "Research only; see reports/futures_gate.csv"
                    ),
                )
            )
        else:
            row.live_enabled = live
            row.note = "Live RANGE book" if live else "Research only; see reports/futures_gate.csv"


def _upsert_run(session: Session, row: pd.Series) -> BacktestRun:
    symbol = str(row["symbol"]).upper()
    window = str(row["window"])
    run = session.scalar(
        select(BacktestRun).where(
            BacktestRun.strategy == "SkyQ RANGE",
            BacktestRun.symbol == symbol,
            BacktestRun.window == window,
        )
    )
    preset = BOOK_PRESETS[symbol]
    note = (
        f"stop={preset.stop_ticks:g}t; floor={preset.min_channel_pct:.2%}; "
        f"max_hold={preset.max_hold_bars}; quantity=1000; RTH; 15:55 flat"
    )
    if run is None:
        run = BacktestRun(symbol=symbol, window=window)
        session.add(run)
    run.start = _dt(row.get("start"))
    run.end = _dt(row.get("end"))
    run.quantity = 1000
    run.starting_equity = 1_000_000.0
    run.preset = note
    run.source_report = f"reports/backtests/skyq_range_{symbol.lower()}_1000sh_2003_2026.txt"
    run.generated_at = datetime.now(tz=timezone.utc)
    session.flush()

    metrics = session.get(BacktestMetric, run.id)
    if metrics is None:
        metrics = BacktestMetric(run_id=run.id)
        session.add(metrics)
    metrics.trades = int(row.get("trades", 0))
    metrics.win_rate = float(row.get("win_rate", 0.0))
    metrics.profit_factor = float(row.get("pf", 0.0))
    metrics.net = float(row.get("net", 0.0))
    metrics.return_pct = float(row.get("return_pct", 0.0))
    metrics.max_drawdown = -abs(float(row.get("max_dd", 0.0)))
    metrics.max_drawdown_pct = abs(float(row.get("max_dd_pct", 0.0)))
    metrics.sharpe = float(row.get("sharpe_d", 0.0))
    metrics.net_per_month = float(row.get("net_per_month", 0.0))
    metrics.positive_months = int(row.get("positive_months", 0))
    metrics.months = int(row.get("months", 0))
    metrics.positive_years = int(row.get("positive_years", 0))
    metrics.years = int(row.get("years", 0))
    metrics.costs = float(row.get("costs", 0.0))
    return run


def _seed_metrics(session: Session) -> None:
    path = PUBLISHED / "range_metrics.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; run scripts/build_published_data.py")
    frame = pd.read_csv(path)
    for _, row in frame.iterrows():
        _upsert_run(session, row)


def _seed_equity(session: Session) -> None:
    path = PUBLISHED / "equity_curve.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; run scripts/build_published_data.py")
    frame = pd.read_csv(path)
    runs = {
        (row.symbol, row.window): row
        for row in session.scalars(select(BacktestRun)).all()
    }
    for (symbol, window), run in runs.items():
        subset = frame[(frame["symbol"] == symbol) & (frame["window"] == window)]
        if subset.empty:
            continue
        session.query(EquityPoint).filter(EquityPoint.run_id == run.id).delete(
            synchronize_session=False
        )
        session.add_all(
            [
                EquityPoint(
                    run_id=run.id,
                    date=_dt(row["date"]),
                    daily_pnl=float(row["daily_pnl"]),
                    equity=float(row["equity"]),
                    drawdown=float(row["drawdown"]),
                )
                for _, row in subset.iterrows()
            ]
        )


def _seed_recent_trades(session: Session, *, refresh: bool = False) -> None:
    path = PUBLISHED / "recent_trades.csv"
    if not path.exists():
        return
    frame = pd.read_csv(path)
    for symbol in ("SPY", "QQQ"):
        run = session.scalar(
            select(BacktestRun).where(
                BacktestRun.symbol == symbol, BacktestRun.window == "2003-2026"
            )
        )
        if run is None:
            continue
        subset = frame[frame["symbol"] == symbol]
        if subset.empty:
            continue
        existing = session.query(Trade).filter(Trade.run_id == run.id).count()
        if existing and not refresh:
            # A full ledger may have been loaded after the initial seed.  Do not
            # silently replace it with the 250-row committed sample.
            continue
        session.query(Trade).filter(Trade.run_id == run.id).delete(synchronize_session=False)
        session.add_all(
            [
                Trade(
                    run_id=run.id,
                    symbol=symbol,
                    side=str(row.get("side", "LONG")).upper(),
                    bar_time=_dt(row.get("bar_time")),
                    signal_time=_dt(row.get("signal_time")),
                    entry_time=_dt(row.get("entry_time")),
                    entry_price=float(row.get("entry_price", 0)),
                    stop_price=float(row.get("stop_price", 0)),
                    target_price=float(row.get("target_price", 0)),
                    exit_time=_dt(row.get("exit_time")),
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
                for _, row in subset.iterrows()
            ]
        )


def _seed_futures(session: Session) -> None:
    path = PUBLISHED / "futures_gate.csv"
    if not path.exists():
        path = ROOT / "reports" / "futures_gate.csv"
    if not path.exists():
        return
    frame = pd.read_csv(path)
    for _, row in frame.iterrows():
        symbol = str(row["symbol"]).upper()
        gate = session.get(FuturesGate, symbol)
        if gate is None:
            gate = FuturesGate(symbol=symbol)
            session.add(gate)
        gate.asset_class = str(row.get("asset_class", "FUT"))
        gate.description = str(row.get("description", ""))
        gate.data_file = str(row.get("data_file", ""))
        gate.bars = int(row.get("bars", 0))
        gate.trades = int(row.get("trades", 0))
        gate.win_rate = float(row.get("win_rate", 0))
        gate.profit_factor = float(row.get("profit_factor", 0))
        gate.net = float(row.get("net", 0))
        gate.net_per_month = float(row.get("net_per_month", 0))
        gate.max_drawdown = float(row.get("max_drawdown", 0))
        gate.positive_years = int(row.get("positive_years", 0))
        gate.years = int(row.get("years", 0))
        gate.verdict = str(row.get("verdict", ""))
        gate.generated_at = datetime.now(tz=timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database-url", default=None)
    parser.add_argument(
        "--refresh-trades",
        action="store_true",
        help="replace existing trade rows with the committed 500-row sample",
    )
    args = parser.parse_args()
    url = args.database_url or os.getenv("DATABASE_URL")
    engine = make_engine(url)
    init_db(engine)
    factory = session_factory(engine)
    with factory() as session:
        _seed_instruments(session)
        _seed_metrics(session)
        session.commit()
        _seed_equity(session)
        _seed_recent_trades(session, refresh=args.refresh_trades)
        _seed_futures(session)
        session.add(
            AuditEvent(
                action="database.seed",
                detail={"database": mask_database_url(str(engine.url))},
            )
        )
        session.commit()
        counts = {
            "instruments": session.query(Instrument).count(),
            "runs": session.query(BacktestRun).count(),
            "equity": session.query(EquityPoint).count(),
            "trades": session.query(Trade).count(),
            "futures": session.query(FuturesGate).count(),
        }
    print(f"database: {mask_database_url(str(engine.url))}")
    print(counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
