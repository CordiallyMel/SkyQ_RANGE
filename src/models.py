"""SQLAlchemy models for the RANGE reporting database.

The schema is intentionally small and explicit: one row per instrument, one
row per reproducible backtest run, one row per trade, and a daily equity curve.
The full 100,000+ trade ledgers can be loaded from their canonical CSVs, while
the dashboard's committed seed contains metrics, daily equity and a recent
sample so a fresh clone has something to render.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    pass


class Instrument(Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(16), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False)
    multiplier: Mapped[float] = mapped_column(Float, nullable=False)
    live_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        UniqueConstraint("strategy", "symbol", "window", name="uq_run_strategy_symbol_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy: Mapped[str] = mapped_column(String(32), default="SkyQ RANGE", nullable=False)
    symbol: Mapped[str] = mapped_column(ForeignKey("instruments.symbol"), nullable=False)
    window: Mapped[str] = mapped_column(String(32), nullable=False)
    start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    starting_equity: Mapped[float] = mapped_column(Float, default=1_000_000.0, nullable=False)
    preset: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    source_report: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    metrics: Mapped["BacktestMetric | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", uselist=False
    )
    trades: Mapped[list["Trade"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )
    equity: Mapped[list["EquityPoint"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class BacktestMetric(Base):
    __tablename__ = "backtest_metrics"

    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), primary_key=True
    )
    trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    net: Mapped[float] = mapped_column(Float, default=0.0)
    return_pct: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe: Mapped[float] = mapped_column(Float, default=0.0)
    net_per_month: Mapped[float] = mapped_column(Float, default=0.0)
    positive_months: Mapped[int] = mapped_column(Integer, default=0)
    months: Mapped[int] = mapped_column(Integer, default=0)
    positive_years: Mapped[int] = mapped_column(Integer, default=0)
    years: Mapped[int] = mapped_column(Integer, default=0)
    costs: Mapped[float] = mapped_column(Float, default=0.0)

    run: Mapped[BacktestRun] = relationship(back_populates="metrics")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_run_exit", "run_id", "exit_time"),
        Index("ix_trades_symbol_exit", "symbol", "exit_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    bar_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    signal_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    stop_price: Mapped[float] = mapped_column(Float, default=0.0)
    target_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    costs: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    bars_held: Mapped[int] = mapped_column(Integer, default=0)
    channel_width: Mapped[float] = mapped_column(Float, default=0.0)
    zone_price: Mapped[float] = mapped_column(Float, default=0.0)

    run: Mapped[BacktestRun] = relationship(back_populates="trades")


class EquityPoint(Base):
    __tablename__ = "equity_points"
    __table_args__ = (
        UniqueConstraint("run_id", "date", name="uq_equity_run_date"),
        Index("ix_equity_run_date", "run_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    daily_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    equity: Mapped[float] = mapped_column(Float, default=1_000_000.0)
    drawdown: Mapped[float] = mapped_column(Float, default=0.0)

    run: Mapped[BacktestRun] = relationship(back_populates="equity")


class FuturesGate(Base):
    __tablename__ = "futures_gate"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    asset_class: Mapped[str] = mapped_column(String(16), default="FUT", nullable=False)
    description: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    data_file: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    bars: Mapped[int] = mapped_column(Integer, default=0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    net: Mapped[float] = mapped_column(Float, default=0.0)
    net_per_month: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    positive_years: Mapped[int] = mapped_column(Integer, default=0)
    years: Mapped[int] = mapped_column(Integer, default=0)
    verdict: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(80), default="local", nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
