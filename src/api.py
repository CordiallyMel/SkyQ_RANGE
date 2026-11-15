"""FastAPI read model and local control endpoints.

The API is intentionally read-only except for the fixed paper-monitor actions
in :mod:`src.control`.  It never returns credentials, environment values, order
payloads, or arbitrary process output.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from integrations import doctor
from src.app_config import AppSettings, mask_database_url
from src.control import ControlError, ControlManager
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

log = logging.getLogger("skyq.range.api")
settings = AppSettings.from_env()
engine = make_engine(settings.database_url)
init_db(engine)
SessionLocal = session_factory(engine)
control = ControlManager(settings)

app = FastAPI(
    title="SkyQ RANGE API",
    version="1.0.0",
    description="Research, risk and forward-testing read model for the RANGE ETF books.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def get_session() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.isoformat()


def _metric(row: BacktestMetric | None) -> dict:
    if row is None:
        return {}
    return {
        "trades": row.trades,
        "win_rate": row.win_rate,
        "profit_factor": row.profit_factor,
        "net": row.net,
        "return_pct": row.return_pct,
        "max_drawdown": row.max_drawdown,
        "max_drawdown_pct": row.max_drawdown_pct,
        "sharpe": row.sharpe,
        "net_per_month": row.net_per_month,
        "positive_months": row.positive_months,
        "months": row.months,
        "positive_years": row.positive_years,
        "years": row.years,
        "costs": row.costs,
    }


def _run(session: Session, symbol: str, window: str) -> BacktestRun | None:
    return session.scalar(
        select(BacktestRun).where(
            BacktestRun.symbol == symbol.upper(),
            BacktestRun.window == window,
            BacktestRun.strategy == "SkyQ RANGE",
        )
    )


def _downsample(points: list[dict], limit: int = 2200) -> list[dict]:
    if len(points) <= limit:
        return points
    step = max(1, len(points) // limit)
    sampled = points[::step]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled


def _monthly(points: list[dict]) -> list[dict]:
    buckets: dict[str, float] = defaultdict(float)
    for point in points:
        day = str(point["date"])[:7]
        buckets[day] += float(point["daily_pnl"])
    return [{"month": month, "net": round(net, 2)} for month, net in sorted(buckets.items())]


@app.get("/api/health")
def health(session: Session = Depends(get_session)) -> dict:
    try:
        session.execute(select(func.count()).select_from(Instrument)).scalar_one()
        db_ok = True
    except Exception:
        log.exception("database health check failed")
        db_ok = False
    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "database_dialect": engine.dialect.name,
        "control_enabled": settings.control_enabled,
    }


@app.get("/api/books")
def books(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.execute(
        select(BacktestRun, BacktestMetric)
        .join(BacktestMetric, BacktestMetric.run_id == BacktestRun.id)
        .order_by(BacktestRun.symbol, BacktestRun.window)
    ).all()
    output: list[dict] = []
    for run, metric in rows:
        output.append(
            {
                "symbol": run.symbol,
                "name": session.get(Instrument, run.symbol).name
                if session.get(Instrument, run.symbol)
                else run.symbol,
                "window": run.window,
                "quantity": run.quantity,
                "starting_equity": run.starting_equity,
                "preset": run.preset,
                "source_report": run.source_report,
                "start": _iso(run.start),
                "end": _iso(run.end),
                "metrics": _metric(metric),
            }
        )
    return output


@app.get("/api/overview")
def overview(
    symbol: str = Query("SPY", pattern="^(SPY|QQQ)$"),
    window: str = Query("2003-2026", pattern="^(2003-2026|2007-2026)$"),
    session: Session = Depends(get_session),
) -> dict:
    run = _run(session, symbol, window)
    if run is None:
        raise HTTPException(404, f"no RANGE run for {symbol} {window}")
    points = [
        {
            "date": _iso(point.date),
            "equity": point.equity,
            "drawdown": point.drawdown,
            "daily_pnl": point.daily_pnl,
        }
        for point in session.scalars(
            select(EquityPoint).where(EquityPoint.run_id == run.id).order_by(EquityPoint.date)
        )
    ]
    instrument = session.get(Instrument, run.symbol)
    return {
        "symbol": run.symbol,
        "name": instrument.name if instrument else run.symbol,
        "window": run.window,
        "start": _iso(run.start),
        "end": _iso(run.end),
        "quantity": run.quantity,
        "starting_equity": run.starting_equity,
        "preset": run.preset,
        "source_report": run.source_report,
        "metrics": _metric(session.get(BacktestMetric, run.id)),
        "equity": _downsample(points),
        "monthly": _monthly(points),
        "trade_count": session.scalar(
            select(func.count()).select_from(Trade).where(Trade.run_id == run.id)
        )
        or 0,
    }


@app.get("/api/trades")
def trades(
    symbol: str = Query("SPY", pattern="^(SPY|QQQ)$"),
    window: str = Query("2003-2026", pattern="^(2003-2026|2007-2026)$"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    side: str | None = Query(None, pattern="^(LONG|SHORT)$"),
    exit_reason: str | None = Query(None, max_length=32),
    session: Session = Depends(get_session),
) -> dict:
    run = _run(session, symbol, window)
    if run is None:
        raise HTTPException(404, f"no RANGE run for {symbol} {window}")
    query = select(Trade).where(Trade.run_id == run.id)
    count_query = select(func.count()).select_from(Trade).where(Trade.run_id == run.id)
    if side:
        query = query.where(Trade.side == side)
        count_query = count_query.where(Trade.side == side)
    if exit_reason:
        query = query.where(Trade.exit_reason == exit_reason)
        count_query = count_query.where(Trade.exit_reason == exit_reason)
    query = query.order_by(Trade.exit_time.desc()).offset(offset).limit(limit)
    rows = session.scalars(query).all()
    return {
        "symbol": symbol,
        "window": window,
        "total": session.scalar(count_query) or 0,
        "limit": limit,
        "offset": offset,
        "rows": [
            {
                "id": row.id,
                "side": row.side,
                "entry_time": _iso(row.entry_time),
                "exit_time": _iso(row.exit_time),
                "entry_price": row.entry_price,
                "exit_price": row.exit_price,
                "stop_price": row.stop_price,
                "target_price": row.target_price,
                "exit_reason": row.exit_reason,
                "quantity": row.quantity,
                "net_pnl": row.net_pnl,
                "costs": row.costs,
                "bars_held": row.bars_held,
                "channel_width": row.channel_width,
            }
            for row in rows
        ],
    }


@app.get("/api/research/futures")
def futures(session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "symbol": row.symbol,
            "asset_class": row.asset_class,
            "description": row.description,
            "bars": row.bars,
            "trades": row.trades,
            "win_rate": row.win_rate,
            "profit_factor": row.profit_factor,
            "net": row.net,
            "net_per_month": row.net_per_month,
            "max_drawdown": row.max_drawdown,
            "positive_years": row.positive_years,
            "years": row.years,
            "verdict": row.verdict,
        }
        for row in session.scalars(select(FuturesGate).order_by(FuturesGate.symbol))
    ]


@app.get("/api/system")
def system(session: Session = Depends(get_session)) -> dict:
    adapter_status = doctor()
    imports = {}
    for name, module in (
        ("ibkr", "ib_async"),
        ("alpaca", "alpaca"),
        ("databento", "databento"),
        ("moomoo", "futu"),
        ("mt5", "MetaTrader5"),
    ):
        try:
            __import__(module)
            imports[name] = True
        except Exception:
            imports[name] = False
    return {
        "app": "skyq-range",
        "version": "1.0.0",
        "database": {
            "dialect": engine.dialect.name,
            "url": mask_database_url(str(engine.url)),
            "instruments": session.scalar(select(func.count()).select_from(Instrument)) or 0,
            "runs": session.scalar(select(func.count()).select_from(BacktestRun)) or 0,
            "trades": session.scalar(select(func.count()).select_from(Trade)) or 0,
            "equity_points": session.scalar(select(func.count()).select_from(EquityPoint)) or 0,
        },
        "brokers_configured": {row.name: row.configured for row in adapter_status},
        "packages": imports,
        "control": control.status(),
        "latest_audit": [
            {
                "at": _iso(event.created_at),
                "action": event.action,
                "actor": event.actor,
            }
            for event in session.scalars(
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(10)
            )
        ],
    }


@app.get("/api/integrations")
def integrations_doctor() -> list[dict]:
    return [row.as_dict() for row in doctor()]


@app.get("/api/control/status")
def control_status() -> dict:
    return control.status()


def _require_local_control(
    request: Request,
    x_skyq_control: str | None = Header(default=None),
    x_skyq_control_token: str | None = Header(default=None, alias="X-SkyQ-Control-Token"),
) -> None:
    if not settings.control_enabled:
        raise HTTPException(403, "control is disabled")
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(403, "control is loopback-only")
    if x_skyq_control != "1":
        raise HTTPException(403, "missing X-SkyQ-Control: 1")
    if settings.control_token and x_skyq_control_token != settings.control_token:
        raise HTTPException(403, "invalid control token")
    origin = request.headers.get("origin")
    if origin and not (
        origin.startswith("http://127.0.0.1")
        or origin.startswith("http://localhost")
    ):
        raise HTTPException(403, "cross-origin control is not permitted")


@app.post("/api/control/start")
def control_start(
    request: Request,
    payload: dict,
    _: None = Depends(_require_local_control),
    session: Session = Depends(get_session),
) -> dict:
    symbol = str(payload.get("symbol") or "SPY").upper()
    try:
        status = control.start(symbol)
    except ControlError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.add(
        AuditEvent(
            action="control.start",
            detail={"symbol": symbol, "mode": "paper-monitor", "pid": status.get("pid")},
        )
    )
    session.commit()
    return status


@app.post("/api/control/stop")
def control_stop(
    _: None = Depends(_require_local_control),
    session: Session = Depends(get_session),
) -> dict:
    try:
        status = control.stop()
    except ControlError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.add(
        AuditEvent(
            action="control.stop",
            detail={"pid": status.get("pid"), "running": status.get("running")},
        )
    )
    session.commit()
    return status


_DIST = Path(__file__).resolve().parents[1] / "dashboard" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="dashboard")
else:

    @app.get("/")
    def root() -> JSONResponse:
        return JSONResponse(
            {
                "name": "SkyQ RANGE API",
                "docs": "/docs",
                "dashboard": "build dashboard/ and restart the API to serve the UI",
            }
        )
