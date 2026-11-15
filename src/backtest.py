"""Replay the SkyQ RANGE channel scalper without a broker.

The live sequence is:

1. A bar closes.
2. The channel lifecycle is updated: broken channels are removed, a new
   channel may be created.
3. If flat, a touch of either entry zone produces a signal.
4. The entry is a market order at the **next** bar's open (Pine's
   ``strategy.entry``), or the zone price in source-reproduction mode.
5. The stop/target bracket is checked from the entry onward.

When 1-minute data is supplied for a higher timeframe, every bar's stop/target
path is walked in 1-minute order.  If one 1-minute bar touches both levels,
stop-first is the default pessimistic fill.  There is no look-ahead: a channel
created at bar ``i`` is only usable for an entry at ``i+1``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

import numpy as np
import pandas as pd

from config.settings import (
    ASSUME_STOP_FIRST,
    ATR_FILTER,
    ATR_MIN,
    BREAKOUT_TICKS,
    COOLDOWN_BARS,
    ENTRY_ZONE,
    FLAT_AT_MINUTE,
    MAX_HOLD_BARS,
    OVERLAP,
    SLIPPAGE_TICKS,
    STOP_TICKS,
    TP_CHANNEL_PCT,
    TP_FIXED_TICKS,
    TP_MODE,
    Instrument,
)
from src.indicators import add_features, gate_ready, session_mask, skip_hours_mask
from src.strategy_logic import (
    Channel,
    EntryPlan,
    Evaluation,
    Side,
    Signal,
    evaluate_signal,
    plan_entry,
    update_channels,
)

log = logging.getLogger(__name__)


@dataclass
class Trade:
    symbol: str
    side: Side
    mode: str
    bar_time: datetime
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    quantity: int
    gross_pnl: float
    costs: float
    net_pnl: float
    atr: float
    channel_top: float
    channel_bottom: float
    channel_width: float
    zone_price: float
    bars_held: int
    stop_points: float
    target_points: float
    reward_risk: float

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0


@dataclass
class OpenPosition:
    plan: EntryPlan
    entry_index: int
    entry_time: datetime
    signal: Signal
    limit_fill: bool = False


@dataclass
class BacktestResult:
    profile: Instrument
    trades: list[Trade] = field(default_factory=list)
    bars: int = 0
    start: datetime | None = None
    end: datetime | None = None
    signal_count: int = 0
    evaluations: int = 0
    channels_created: int = 0
    gate_counts: dict[str, int] = field(default_factory=dict)

    @property
    def net_pnl(self) -> float:
        return sum(trade.net_pnl for trade in self.trades)

    @property
    def gross_pnl(self) -> float:
        return sum(trade.gross_pnl for trade in self.trades)

    @property
    def costs(self) -> float:
        return sum(trade.costs for trade in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for trade in self.trades if trade.net_pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for trade in self.trades if trade.net_pnl < 0)

    @property
    def flats(self) -> int:
        return sum(1 for trade in self.trades if trade.net_pnl == 0)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.trades) if self.trades else 0.0

    @property
    def avg_win(self) -> float:
        winners = [trade.net_pnl for trade in self.trades if trade.net_pnl > 0]
        return float(np.mean(winners)) if winners else 0.0

    @property
    def avg_loss(self) -> float:
        losers = [trade.net_pnl for trade in self.trades if trade.net_pnl < 0]
        return float(np.mean(losers)) if losers else 0.0

    @property
    def reward_risk(self) -> float:
        if self.avg_loss == 0:
            return float("inf") if self.avg_win > 0 else 0.0
        return self.avg_win / abs(self.avg_loss)

    @property
    def profit_factor(self) -> float:
        gross_wins = sum(trade.net_pnl for trade in self.trades if trade.net_pnl > 0)
        gross_losses = abs(sum(trade.net_pnl for trade in self.trades if trade.net_pnl < 0))
        if gross_losses == 0:
            return float("inf") if gross_wins > 0 else 0.0
        return gross_wins / gross_losses

    @property
    def expectancy(self) -> float:
        return self.net_pnl / len(self.trades) if self.trades else 0.0

    @property
    def max_drawdown(self) -> float:
        if not self.trades:
            return 0.0
        equity = np.cumsum([trade.net_pnl for trade in self.trades])
        return float(np.max(np.maximum.accumulate(equity) - equity))

    @property
    def avg_bars(self) -> float:
        return float(np.mean([trade.bars_held for trade in self.trades])) if self.trades else 0.0

    @property
    def exposure(self) -> float:
        if self.bars <= 0:
            return 0.0
        return sum(max(1, trade.bars_held) for trade in self.trades) / self.bars

    @property
    def trading_days(self) -> int:
        return len({trade.entry_time.date() for trade in self.trades})

    @property
    def trades_per_day(self) -> float:
        return len(self.trades) / self.trading_days if self.trading_days else 0.0

    @property
    def avg_channel_width(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([trade.channel_width for trade in self.trades]))

    @property
    def avg_risk_dollars(self) -> float:
        if not self.trades:
            return 0.0
        return float(np.mean([trade.stop_points * trade.quantity * self.profile.point_value for trade in self.trades]))

    def daily_pnl(self) -> pd.Series:
        """Business-day P&L, with weekend exits rolled into Monday."""
        if not self.trades:
            return pd.Series(dtype=float)
        frame = pd.DataFrame(
            {
                "date": [pd.Timestamp(trade.exit_time).date() for trade in self.trades],
                "net": [trade.net_pnl for trade in self.trades],
            }
        )
        daily = frame.groupby("date")["net"].sum()
        daily.index = pd.to_datetime(daily.index)
        if self.start is not None and self.end is not None:
            index = pd.date_range(
                pd.Timestamp(self.start).date(),
                pd.Timestamp(self.end).date(),
                freq="B",
            )
        else:
            index = pd.date_range(daily.index.min(), daily.index.max(), freq="B")
        if index.empty:
            return pd.Series(dtype=float)
        series = pd.Series(0.0, index=index, dtype=float)
        for stamp, value in daily.items():
            target = pd.Timestamp(stamp)
            while target.weekday() >= 5:
                target += pd.Timedelta(days=1)
            if target > index[-1]:
                target = index[-1]
            if target < index[0]:
                target = index[0]
            series.loc[target] += float(value)
        return series

    def trades_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "symbol": trade.symbol,
                    "side": trade.side.value,
                    "mode": trade.mode,
                    "bar_time": trade.bar_time,
                    "signal_time": trade.signal_time,
                    "entry_time": trade.entry_time,
                    "entry_price": trade.entry_price,
                    "stop_price": trade.stop_price,
                    "target_price": trade.target_price,
                    "exit_time": trade.exit_time,
                    "exit_price": trade.exit_price,
                    "exit_reason": trade.exit_reason,
                    "quantity": trade.quantity,
                    "gross_pnl": trade.gross_pnl,
                    "costs": trade.costs,
                    "net_pnl": trade.net_pnl,
                    "atr": trade.atr,
                    "channel_top": trade.channel_top,
                    "channel_bottom": trade.channel_bottom,
                    "channel_width": trade.channel_width,
                    "zone_price": trade.zone_price,
                    "bars_held": trade.bars_held,
                    "stop_points": trade.stop_points,
                    "target_points": trade.target_points,
                    "reward_risk": trade.reward_risk,
                }
                for trade in self.trades
            ]
        )

    def side_frame(self) -> pd.DataFrame:
        columns = [
            "side", "trades", "wins", "losses", "win_rate", "net",
            "avg_win", "avg_loss", "pf",
        ]
        if not self.trades:
            return pd.DataFrame(columns=columns)
        frame = self.trades_frame()
        rows = []
        for side in ("LONG", "SHORT"):
            group = frame[frame["side"] == side]
            if group.empty:
                continue
            wins = group[group["net_pnl"] > 0]
            losses = group[group["net_pnl"] < 0]
            rows.append(
                {
                    "side": side,
                    "trades": len(group),
                    "wins": len(wins),
                    "losses": len(losses),
                    "win_rate": len(wins) / len(group),
                    "net": group["net_pnl"].sum(),
                    "avg_win": wins["net_pnl"].mean() if not wins.empty else 0.0,
                    "avg_loss": losses["net_pnl"].mean() if not losses.empty else 0.0,
                    "pf": wins["net_pnl"].sum() / abs(losses["net_pnl"].sum())
                    if not losses.empty and losses["net_pnl"].sum() != 0
                    else 0.0,
                }
            )
        return pd.DataFrame(rows, columns=columns)

    def monthly_frame(self) -> pd.DataFrame:
        columns = ["month", "trades", "wins", "losses", "win_rate", "net"]
        if not self.trades:
            return pd.DataFrame(columns=columns)
        frame = self.trades_frame()
        exits = pd.to_datetime(frame["exit_time"], utc=True).dt.tz_convert("America/New_York")
        frame["month"] = exits.dt.to_period("M").astype(str)
        grouped = frame.groupby("month").agg(
            trades=("net_pnl", "size"),
            wins=("net_pnl", lambda values: int((values > 0).sum())),
            losses=("net_pnl", lambda values: int((values < 0).sum())),
            net=("net_pnl", "sum"),
        )
        grouped["win_rate"] = grouped["wins"] / grouped["trades"]
        return grouped.reset_index().loc[:, columns]

    def yearly_frame(self) -> pd.DataFrame:
        columns = ["year", "trades", "wins", "losses", "win_rate", "net"]
        if not self.trades:
            return pd.DataFrame(columns=columns)
        frame = self.trades_frame()
        entries = pd.to_datetime(frame["entry_time"], utc=True).dt.tz_convert("America/New_York")
        frame["year"] = entries.dt.year
        grouped = frame.groupby("year").agg(
            trades=("net_pnl", "size"),
            wins=("net_pnl", lambda values: int((values > 0).sum())),
            losses=("net_pnl", lambda values: int((values < 0).sum())),
            net=("net_pnl", "sum"),
        )
        grouped["win_rate"] = grouped["wins"] / grouped["trades"]
        return grouped.reset_index().loc[:, columns]

    def summary(self) -> dict[str, float]:
        daily = self.daily_pnl()
        if daily.size > 1:
            std = float(daily.std(ddof=1))
            sharpe = float(daily.mean() / std * np.sqrt(252)) if std > 0 else 0.0
            downside = np.minimum(daily.to_numpy(dtype=float), 0.0)
            dstd = float(np.sqrt(np.mean(np.square(downside)))) if daily.size else 0.0
            sortino = float(daily.mean() / dstd * np.sqrt(252)) if dstd > 0 else 0.0
        else:
            sharpe = 0.0
            sortino = 0.0
        months = 0.0
        if self.start is not None and self.end is not None:
            months = max(
                1.0,
                (pd.Timestamp(self.end) - pd.Timestamp(self.start)).days / 30.44,
            )
        yearly = self.yearly_frame()
        positive_years = int((yearly["net"] > 0).sum()) if not yearly.empty else 0
        monthly = self.monthly_frame()
        positive_months = int((monthly["net"] > 0).sum()) if not monthly.empty else 0
        return {
            "trades": float(len(self.trades)),
            "wins": float(self.wins),
            "losses": float(self.losses),
            "win_rate": self.win_rate,
            "gross": self.gross_pnl,
            "costs": self.costs,
            "net": self.net_pnl,
            "avg_win": self.avg_win,
            "avg_loss": self.avg_loss,
            "rr": self.reward_risk,
            "pf": self.profit_factor,
            "expectancy": self.expectancy,
            "max_drawdown": self.max_drawdown,
            "avg_bars": self.avg_bars,
            "exposure": self.exposure,
            "trading_days": float(self.trading_days),
            "trades_per_day": self.trades_per_day,
            "net_per_month": self.net_pnl / months if months else 0.0,
            "trades_per_month": len(self.trades) / months if months else 0.0,
            "sharpe": sharpe,
            "sortino": sortino,
            "positive_years": float(positive_years),
            "years": float(len(yearly)),
            "positive_months": float(positive_months),
            "months": float(len(monthly)),
            "signals": float(self.signal_count),
            "channels": float(self.channels_created),
            "avg_channel_width": self.avg_channel_width,
            "avg_risk": self.avg_risk_dollars,
        }


def _pnl(side: Side, entry: float, exit_price: float, point_value: float, quantity: int) -> float:
    return (exit_price - entry) * side.direction * point_value * quantity


def _commission(profile: Instrument, quantity: int, commission_per_contract: float | None) -> float:
    """Round-trip commission for one position."""
    if profile.is_future:
        rate = (
            float(commission_per_contract)
            if commission_per_contract is not None
            else float(profile.commission_per_contract)
        )
        return rate * quantity * 2.0
    per_share = float(profile.commission_per_share) * quantity * 2.0
    return max(float(profile.min_commission_per_order) * 2.0, per_share)


def _costs(
    *,
    profile: Instrument,
    quantity: int,
    exit_reason: str,
    exit_slippage_ticks: float,
    commission_per_contract: float | None,
) -> float:
    """Commission plus the exit-side slippage.

    The entry slippage is already baked into ``entry_price``.  A resting limit
    target is not slipped; a stop, time or flat exit is a market order and is.
    """
    commission = _commission(profile, quantity, commission_per_contract)
    slipped = 0.0 if exit_reason == "TARGET" else exit_slippage_ticks
    slippage = slipped * profile.tick_size * profile.point_value * quantity
    return commission + slippage


def _entry_bar_exit(
    position: OpenPosition,
    high: float,
    low: float,
    close: float,
    *,
    assume_stop_first: bool,
) -> tuple[str, float] | None:
    """Conservative bracket check on the bar where a zone limit filled.

    A limit buy at the zone fills somewhere inside the bar.  The bar's high may
    have printed *before* that fill, so the target is only credited when the
    bar closes beyond it.  A stop touch is always credited, which is the
    pessimistic assumption.  This removes the intrabar look-ahead that makes a
    naive zone-fill backtest look far better than the live order path.
    """
    plan = position.plan
    stop = plan.stop_price
    target = plan.target_price
    if plan.side is Side.LONG:
        if low <= stop:
            return "STOP", stop
        if close >= target:
            return "TARGET", target
        return None
    if high >= stop:
        return "STOP", stop
    if close <= target:
        return "TARGET", target
    return None


def _bar_exit(
    position: OpenPosition,
    open_: float,
    high: float,
    low: float,
    *,
    assume_stop_first: bool,
) -> tuple[str, float] | None:
    """Return the bracket fill if the bar's open/range hits stop or target."""
    plan = position.plan
    side = plan.side
    stop = plan.stop_price
    target = plan.target_price

    if side is Side.LONG:
        if open_ <= stop:
            return "STOP", open_
        if open_ >= target:
            return "TARGET", open_
        stop_touched = low <= stop
        target_touched = high >= target
        if stop_touched and target_touched:
            return ("STOP", stop) if assume_stop_first else ("TARGET", target)
        if stop_touched:
            return "STOP", stop
        if target_touched:
            return "TARGET", target
        return None

    if open_ >= stop:
        return "STOP", open_
    if open_ <= target:
        return "TARGET", open_
    stop_touched = high >= stop
    target_touched = low <= target
    if stop_touched and target_touched:
        return ("STOP", stop) if assume_stop_first else ("TARGET", target)
    if stop_touched:
        return "STOP", stop
    if target_touched:
        return "TARGET", target
    return None


class _Path:
    """A lightweight view over the 1-minute bars used for intrabar fills."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.index = frame.index
        self.open = frame["open"].to_numpy(dtype=float)
        self.high = frame["high"].to_numpy(dtype=float)
        self.low = frame["low"].to_numpy(dtype=float)
        self.close = frame["close"].to_numpy(dtype=float)

    def bounds(self, start: pd.Timestamp, stop: pd.Timestamp) -> tuple[int, int]:
        start_value = start.value if start.tzinfo is not None else start.tz_localize("America/New_York").value
        stop_value = stop.value if stop.tzinfo is not None else stop.tz_localize("America/New_York").value
        left = int(np.searchsorted(self.index.asi8, start_value, side="left"))
        right = int(np.searchsorted(self.index.asi8, stop_value, side="left"))
        return left, right


def _path_exit(
    position: OpenPosition,
    path: _Path,
    left: int,
    right: int,
    *,
    assume_stop_first: bool,
) -> tuple[str, float, datetime] | None:
    for offset in range(left, right):
        hit = _bar_exit(
            position,
            float(path.open[offset]),
            float(path.high[offset]),
            float(path.low[offset]),
            assume_stop_first=assume_stop_first,
        )
        if hit is not None:
            stamp = pd.Timestamp(path.index[offset]) + pd.Timedelta(minutes=1)
            return hit[0], hit[1], stamp.to_pydatetime()
    return None


def _record_trade(
    result: BacktestResult,
    position: OpenPosition,
    *,
    exit_time: datetime,
    exit_price: float,
    exit_reason: str,
    bars_held: int,
    exit_slippage_ticks: float,
    commission_per_contract: float | None,
) -> Trade:
    plan = position.plan
    gross = _pnl(
        plan.side,
        plan.entry_price,
        exit_price,
        plan.point_value,
        plan.quantity,
    )
    costs = _costs(
        profile=result.profile,
        quantity=plan.quantity,
        exit_reason=exit_reason,
        exit_slippage_ticks=exit_slippage_ticks,
        commission_per_contract=commission_per_contract,
    )
    signal = position.signal
    trade = Trade(
        symbol=signal.symbol,
        side=plan.side,
        mode=signal.mode,
        bar_time=signal.bar_time,
        signal_time=signal.signal_time,
        entry_time=position.entry_time,
        entry_price=plan.entry_price,
        stop_price=plan.stop_price,
        target_price=plan.target_price,
        exit_time=exit_time,
        exit_price=float(exit_price),
        exit_reason=exit_reason,
        quantity=plan.quantity,
        gross_pnl=gross,
        costs=costs,
        net_pnl=gross - costs,
        atr=signal.atr,
        channel_top=signal.channel.top,
        channel_bottom=signal.channel.bottom,
        channel_width=signal.channel.width,
        zone_price=signal.zone_price,
        bars_held=max(1, int(bars_held)),
        stop_points=plan.stop_distance,
        target_points=plan.target_distance,
        reward_risk=plan.reward_risk,
    )
    result.trades.append(trade)
    return trade


def _slice_frame(
    data: pd.DataFrame,
    start: str | None,
    end: str | None,
) -> pd.DataFrame:
    if start is not None:
        start_ts = pd.Timestamp(start)
        start_ts = (
            start_ts.tz_localize(data.index.tz)
            if start_ts.tzinfo is None
            else start_ts.tz_convert(data.index.tz)
        )
        data = data[data.index >= start_ts]
    if end is not None:
        end_ts = pd.Timestamp(end)
        end_ts = (
            end_ts.tz_localize(data.index.tz)
            if end_ts.tzinfo is None
            else end_ts.tz_convert(data.index.tz)
        )
        data = data[data.index < end_ts + pd.Timedelta(days=1)]
    return data


def run_backtest(
    frame: pd.DataFrame,
    profile: Instrument,
    *,
    start: str | None = None,
    end: str | None = None,
    sides: str = "both",
    quantity: int = 1,
    entry_zone: float = ENTRY_ZONE,
    min_channel_width: float = 0.0,
    min_channel_pct: float | None = None,
    max_channel_width: float | None = None,
    stop_ticks: float = STOP_TICKS,
    tp_mode: str = TP_MODE,
    tp_channel_pct: float = TP_CHANNEL_PCT,
    tp_fixed_ticks: float = TP_FIXED_TICKS,
    cooldown_bars: int = COOLDOWN_BARS,
    overlap: bool = OVERLAP,
    breakout_ticks: float = BREAKOUT_TICKS,
    atr_filter: bool = ATR_FILTER,
    atr_min: float = ATR_MIN,
    atr_percentile: float | None = None,
    session: str = "all",
    window_start_minute: int | None = None,
    window_end_minute: int | None = None,
    skip_hours: tuple[int, ...] = (),
    event_codes: tuple[str, ...] = (),
    calendar: pd.DataFrame | None = None,
    allow_creation_entry: bool = True,
    require_inside: bool = False,
    ambiguous: str = "long",
    entry_mode: str = "next_open",
    entry_slippage_ticks: float = SLIPPAGE_TICKS,
    exit_slippage_ticks: float = SLIPPAGE_TICKS,
    commission_per_contract: float | None = None,
    max_hold_bars: int = MAX_HOLD_BARS,
    flat_at_minute: int | None = FLAT_AT_MINUTE,
    timeframe: str = "5m",
    intrabar: pd.DataFrame | None = None,
    assume_stop_first: bool = ASSUME_STOP_FIRST,
    remove_one_breakout: bool = False,
    breakout_blocks_entry: bool = False,
    max_channels: int = 500,
) -> BacktestResult:
    """Replay every completed bar in ``frame`` for one instrument."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if stop_ticks < 0:
        raise ValueError("stop_ticks must be non-negative")
    if not 0.0 < entry_zone < 0.5:
        raise ValueError("entry_zone must be between 0 and 0.5")
    if entry_mode not in {"next_open", "zone", "close"}:
        raise ValueError("entry_mode must be next_open, zone or close")
    if frame.empty:
        return BacktestResult(profile=profile)

    data = frame.copy()
    if "channel_event" not in data.columns:
        data = add_features(data, timeframe=timeframe)
    data = gate_ready(data)
    data = _slice_frame(data, start, end)
    if data.empty:
        return BacktestResult(profile=profile)

    result = BacktestResult(
        profile=profile,
        bars=len(data),
        start=data.index[0].to_pydatetime(),
        end=data.index[-1].to_pydatetime(),
    )

    path = None
    if intrabar is not None and not intrabar.empty and timeframe != "1m":
        usable = intrabar[intrabar.index >= data.index[0]]
        usable = usable[usable.index < data.index[-1] + pd.Timedelta(minutes=60)]
        if not usable.empty:
            path = _Path(usable)

    open_ = data["open"].to_numpy(dtype=float)
    high = data["high"].to_numpy(dtype=float)
    low = data["low"].to_numpy(dtype=float)
    close = data["close"].to_numpy(dtype=float)
    events = data["channel_event"].to_numpy(dtype=bool)
    tops = data["channel_top"].to_numpy(dtype=float)
    bottoms = data["channel_bottom"].to_numpy(dtype=float)
    bar_closes = data["bar_close"].to_numpy() if "bar_close" in data.columns else None
    index_values = data.index
    atr_values = (
        data["atr_1m"].to_numpy(dtype=float)
        if "atr_1m" in data.columns
        else data["atr"].to_numpy(dtype=float)
    )
    minute_values = (index_values.hour * 60 + index_values.minute).to_numpy(dtype=int)
    session_ok = session_mask(index_values, session)
    if window_start_minute is not None and window_end_minute is not None:
        if window_start_minute <= window_end_minute:
            window_ok = (minute_values >= window_start_minute) & (minute_values < window_end_minute)
        else:
            window_ok = (minute_values >= window_start_minute) | (minute_values < window_end_minute)
        session_ok = session_ok & window_ok
    hour_ok = skip_hours_mask(index_values, skip_hours)
    if event_codes and calendar is not None and not calendar.empty:
        columns = [code for code in event_codes if code in calendar.columns]
        if columns:
            flags = calendar.loc[:, columns].fillna(0).astype(int).sum(axis=1)
            bad_days = {pd.Timestamp(day).date() for day in flags.index[flags > 0]}
            event_ok = np.array([stamp.date() not in bad_days for stamp in index_values])
        else:
            event_ok = np.ones(len(data), dtype=bool)
    else:
        event_ok = np.ones(len(data), dtype=bool)
    threshold = atr_percentile if atr_percentile is not None else atr_min
    atr_ok = atr_values >= threshold if atr_filter else np.ones(len(data), dtype=bool)
    breakout_tolerance = breakout_ticks * profile.tick_size

    channels: list[Channel] = []
    position: OpenPosition | None = None
    pending: Signal | None = None
    last_long = -10**9
    last_short = -10**9
    bars = len(data)

    def note(reason: str | None) -> None:
        if reason:
            result.gate_counts[reason] = result.gate_counts.get(reason, 0) + 1

    def close_time(i: int) -> datetime:
        if bar_closes is not None:
            return pd.Timestamp(bar_closes[i]).to_pydatetime()
        stamp = pd.Timestamp(index_values[i])
        return (stamp + pd.Timedelta(minutes=1)).to_pydatetime()

    for index in range(bars):
        bar_time = None
        bar_close = None

        # 1. A signal from the previous completed bar fills on this bar.
        if pending is not None and position is None:
            signal = pending
            fill: float | None = None
            limit_fill = False
            if entry_mode == "zone":
                zone = signal.zone_price
                if signal.side is Side.LONG:
                    if low[index] > zone:
                        note("no_fill")
                    elif open_[index] <= zone:
                        fill = float(open_[index])
                    else:
                        fill = zone
                        limit_fill = True
                else:
                    if high[index] < zone:
                        note("no_fill")
                    elif open_[index] >= zone:
                        fill = float(open_[index])
                    else:
                        fill = zone
                        limit_fill = True
            elif entry_mode == "close":
                fill = signal.close + signal.side.direction * entry_slippage_ticks * profile.tick_size
            else:
                fill = open_[index] + signal.side.direction * entry_slippage_ticks * profile.tick_size
            if fill is not None:
                try:
                    plan = plan_entry(
                        signal,
                        fill,
                        profile,
                        quantity=quantity,
                        stop_ticks=stop_ticks,
                        tp_mode=tp_mode,
                        tp_channel_pct=tp_channel_pct,
                        tp_fixed_ticks=tp_fixed_ticks,
                    )
                except ValueError as exc:
                    log.debug("Skipping %s entry: %s", signal.symbol, exc)
                    note("invalid_bracket")
                    plan = None
                if plan is not None:
                    position = OpenPosition(
                        plan=plan,
                        entry_index=index,
                        entry_time=pd.Timestamp(index_values[index]).to_pydatetime(),
                        signal=signal,
                        limit_fill=limit_fill,
                    )
            pending = None

        # 2. Bracket / time / flat exits on this bar.
        if position is not None:
            hit = None
            if position.limit_fill and index == position.entry_index:
                # The limit filled somewhere inside the entry bar.  Credit a
                # target only when the bar closes beyond it; always credit a
                # stop touch.  This is the pessimistic, no-look-ahead case.
                hit = _entry_bar_exit(
                    position,
                    float(high[index]),
                    float(low[index]),
                    float(close[index]),
                    assume_stop_first=assume_stop_first,
                )
                if hit is not None:
                    hit = hit[0], hit[1], close_time(index)
            else:
                if path is not None:
                    # Only the 1-minute bars belonging to this chart bar.
                    bar_time_ts = pd.Timestamp(index_values[index])
                    tf_minutes = (
                        int(round((pd.Timestamp(bar_closes[index]) - bar_time_ts).total_seconds() / 60))
                        if bar_closes is not None
                        else 1
                    )
                    left, right = path.bounds(bar_time_ts, bar_time_ts + pd.Timedelta(minutes=tf_minutes))
                    if right > left:
                        hit = _path_exit(
                            position,
                            path,
                            left,
                            right,
                            assume_stop_first=assume_stop_first,
                        )
                if hit is None:
                    hit = _bar_exit(
                        position,
                        float(open_[index]),
                        float(high[index]),
                        float(low[index]),
                        assume_stop_first=assume_stop_first,
                    )
                    if hit is not None:
                        hit = hit[0], hit[1], close_time(index)
            if hit is not None:
                reason, price, exit_time = hit
                _record_trade(
                    result,
                    position,
                    exit_time=exit_time,
                    exit_price=price,
                    exit_reason=reason,
                    bars_held=index - position.entry_index + 1,
                    exit_slippage_ticks=exit_slippage_ticks,
                    commission_per_contract=commission_per_contract,
                )
                position = None
            elif max_hold_bars > 0 and (index - position.entry_index + 1) >= max_hold_bars:
                _record_trade(
                    result,
                    position,
                    exit_time=close_time(index),
                    exit_price=float(close[index]),
                    exit_reason="MAX_HOLD",
                    bars_held=index - position.entry_index + 1,
                    exit_slippage_ticks=exit_slippage_ticks,
                    commission_per_contract=commission_per_contract,
                )
                position = None
            elif flat_at_minute is not None and minute_values[index] >= flat_at_minute:
                _record_trade(
                    result,
                    position,
                    exit_time=close_time(index),
                    exit_price=float(close[index]),
                    exit_reason="FLAT",
                    bars_held=index - position.entry_index + 1,
                    exit_slippage_ticks=exit_slippage_ticks,
                    commission_per_contract=commission_per_contract,
                )
                position = None

        # 3. Channel lifecycle at the close (inlined for the research loop).
        close_i = float(close[index])
        blocked = False
        if channels:
            if remove_one_breakout:
                for offset, channel in enumerate(channels):
                    if close_i > channel.top + breakout_tolerance or close_i < channel.bottom - breakout_tolerance:
                        channels = channels[:offset] + channels[offset + 1 :]
                        blocked = True
                        break
            else:
                kept: list[Channel] = []
                for channel in channels:
                    if close_i > channel.top + breakout_tolerance or close_i < channel.bottom - breakout_tolerance:
                        blocked = True
                    else:
                        kept.append(channel)
                channels = kept
        if events[index]:
            result.channels_created += 1
            top_i = float(tops[index])
            bottom_i = float(bottoms[index])
            width_i = top_i - bottom_i
            if math.isfinite(width_i) and width_i > 0:
                allowed = True
                if not overlap:
                    for channel in channels:
                        if top_i > channel.bottom and bottom_i < channel.top:
                            allowed = False
                            break
                if allowed:
                    channels.insert(
                        0,
                        Channel(
                            top=top_i,
                            bottom=bottom_i,
                            created_index=index,
                            created_time=pd.Timestamp(index_values[index]).to_pydatetime(),
                            width=width_i,
                        ),
                    )
                    if len(channels) > max_channels:
                        channels.pop()

        # 4. A new setup is evaluated only when flat and with a next bar to fill.
        if position is None and pending is None and index + 1 < bars:
            result.evaluations += 1
            if breakout_blocks_entry and blocked:
                note("breakout")
                continue
            if not session_ok[index]:
                note("session")
                continue
            if not hour_ok[index]:
                note("hour")
                continue
            if not event_ok[index]:
                note("event")
                continue
            if not atr_ok[index]:
                note("atr")
                continue
            if not channels:
                note("no_channel")
                continue

            long_allowed = sides in {"long", "both"} and index - last_long >= cooldown_bars
            short_allowed = sides in {"short", "both"} and index - last_short >= cooldown_bars
            if not long_allowed and not short_allowed:
                note("cooldown")
                continue

            low_i = float(low[index])
            high_i = float(high[index])
            close_i = float(close[index])
            long_hit: tuple[Channel, float] | None = None
            short_hit: tuple[Channel, float] | None = None
            for channel in channels:
                if channel.width <= 0:
                    continue
                if channel.width < min_channel_width:
                    continue
                if min_channel_pct is not None and channel.width < close_i * min_channel_pct:
                    continue
                if max_channel_width is not None and channel.width > max_channel_width:
                    continue
                if not allow_creation_entry and channel.created_index == index:
                    continue
                if require_inside and not (channel.bottom <= close_i <= channel.top):
                    continue
                long_zone = channel.bottom + channel.width * entry_zone
                short_zone = channel.top - channel.width * entry_zone
                if long_allowed and long_hit is None and low_i <= long_zone:
                    long_hit = (channel, long_zone)
                if short_allowed and short_hit is None and high_i >= short_zone:
                    short_hit = (channel, short_zone)
                if long_hit is not None and short_hit is not None:
                    break

            if long_hit is None and short_hit is None:
                note("no_touch")
                continue
            if long_hit is not None and short_hit is not None:
                if ambiguous == "skip":
                    note("ambiguous")
                    continue
                chosen = "long" if ambiguous == "long" else "short"
            else:
                chosen = "long" if long_hit is not None else "short"

            if chosen == "long":
                channel, zone = long_hit  # type: ignore[misc]
                side = Side.LONG
            else:
                channel, zone = short_hit  # type: ignore[misc]
                side = Side.SHORT
            bar_time_ts = pd.Timestamp(index_values[index])
            if bar_closes is not None:
                signal_close = pd.Timestamp(bar_closes[index]).to_pydatetime()
            else:
                signal_close = (bar_time_ts + pd.Timedelta(minutes=1)).to_pydatetime()
            signal = Signal(
                symbol=profile.symbol,
                side=side,
                bar_time=bar_time_ts.to_pydatetime(),
                signal_time=signal_close,
                channel=channel,
                zone_price=float(zone),
                low=low_i,
                high=high_i,
                close=close_i,
                atr=float(atr_values[index]),
            )
            result.signal_count += 1
            pending = signal
            if side is Side.LONG:
                last_long = index
            else:
                last_short = index

    if position is not None:
        last_index = bars - 1
        last_close = pd.Timestamp(index_values[last_index]).to_pydatetime()
        if bar_closes is not None:
            last_close = pd.Timestamp(bar_closes[last_index]).to_pydatetime()
        _record_trade(
            result,
            position,
            exit_time=last_close,
            exit_price=float(close[last_index]),
            exit_reason="END",
            bars_held=bars - position.entry_index,
            exit_slippage_ticks=exit_slippage_ticks,
            commission_per_contract=commission_per_contract,
        )

    return result


def aggregate_results(results: Iterable[BacktestResult]) -> dict[str, float]:
    """Aggregate several instruments with trades in exit-time order."""
    results = list(results)
    trades = sorted(
        (trade for result in results for trade in result.trades),
        key=lambda trade: trade.exit_time,
    )
    if not trades:
        return {
            "trades": 0.0,
            "wins": 0.0,
            "losses": 0.0,
            "win_rate": 0.0,
            "gross": 0.0,
            "costs": 0.0,
            "net": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "rr": 0.0,
            "pf": 0.0,
            "expectancy": 0.0,
            "max_drawdown": 0.0,
            "avg_bars": 0.0,
            "exposure": 0.0,
        }
    wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    equity = np.cumsum([trade.net_pnl for trade in trades])
    drawdown = float(np.max(np.maximum.accumulate(equity) - equity))
    return {
        "trades": float(len(trades)),
        "wins": float(len(wins)),
        "losses": float(len(losses)),
        "win_rate": len(wins) / len(trades),
        "gross": sum(trade.gross_pnl for trade in trades),
        "costs": sum(trade.costs for trade in trades),
        "net": sum(trade.net_pnl for trade in trades),
        "avg_win": float(np.mean(wins)) if wins else 0.0,
        "avg_loss": float(np.mean(losses)) if losses else 0.0,
        "rr": (float(np.mean(wins)) / abs(float(np.mean(losses))))
        if wins and losses
        else 0.0,
        "pf": (sum(wins) / abs(sum(losses))) if losses else 0.0,
        "expectancy": sum(trade.net_pnl for trade in trades) / len(trades),
        "max_drawdown": drawdown,
        "avg_bars": float(np.mean([trade.bars_held for trade in trades])),
        "exposure": float(
            sum(max(1, trade.bars_held) for trade in trades)
            / max(1, sum(result.bars for result in results))
        ),
    }
