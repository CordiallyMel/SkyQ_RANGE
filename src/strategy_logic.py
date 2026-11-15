"""Broker-free signal rules for the SkyQ RANGE channel scalper.

A channel is created when the normalized-price volatility switches from a
low-volatility extreme back to a high-volatility extreme, using the running
high/low since the low-volatility crossover.  The channel then stays active
until a close is more than ``breakout_ticks`` outside it.

Entries are mean-reversion touches:

* long when the bar low enters the bottom ``entry_zone`` fraction;
* short when the bar high enters the top ``entry_zone`` fraction.

A touch is evaluated on a completed bar.  The executable entry is the next
bar's open (the Pine ``strategy.entry`` behaviour), or the zone price when the
research harness is reproducing the source's optimistic reference fill.  The
stop is ``stop_ticks`` beyond the channel edge; the target is a fraction of the
channel range from the entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import pandas as pd

from config.settings import (
    Instrument,
    NY_TZ,
    SIDES_CHOICES,
    SESSION_CHOICES,
)
from src.indicators import event_on, session_allows, skip_hours_mask


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def action(self) -> str:
        return "BUY" if self is Side.LONG else "SELL"

    @property
    def exit_action(self) -> str:
        return "SELL" if self is Side.LONG else "BUY"

    @property
    def direction(self) -> float:
        return 1.0 if self is Side.LONG else -1.0


@dataclass(frozen=True)
class Channel:
    top: float
    bottom: float
    created_index: int
    created_time: datetime
    width: float

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2.0

    def is_broken(self, close: float, tick_size: float, breakout_ticks: float) -> bool:
        tolerance = breakout_ticks * tick_size
        return close > self.top + tolerance or close < self.bottom - tolerance


@dataclass(frozen=True)
class Signal:
    """A completed-bar touch; the next open is the executable entry."""

    symbol: str
    side: Side
    bar_time: datetime
    signal_time: datetime
    channel: Channel
    zone_price: float
    low: float
    high: float
    close: float
    atr: float
    mode: str = "bocs"

    @property
    def channel_width(self) -> float:
        return self.channel.width

    def describe(self) -> str:
        return (
            f"{self.symbol} {self.side.value} touch={self.zone_price:.2f} "
            f"channel={self.channel.bottom:.2f}-{self.channel.top:.2f} "
            f"bar={self.bar_time.isoformat()} atr={self.atr:.2f}"
        )


@dataclass(frozen=True)
class Evaluation:
    signal: Signal | None = None
    reason: str | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.signal is not None


@dataclass(frozen=True)
class EntryPlan:
    signal: Signal
    entry_price: float
    stop_price: float
    target_price: float
    quantity: int
    point_value: float
    tick_size: float
    channel_width: float

    @property
    def side(self) -> Side:
        return self.signal.side

    @property
    def stop_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)

    @property
    def target_distance(self) -> float:
        return abs(self.target_price - self.entry_price)

    @property
    def reward_risk(self) -> float:
        return self.target_distance / self.stop_distance if self.stop_distance > 0 else 0.0

    @property
    def risk_dollars(self) -> float:
        return self.stop_distance * self.quantity * self.point_value

    @property
    def reward_dollars(self) -> float:
        return self.target_distance * self.quantity * self.point_value

    def describe(self) -> str:
        return (
            f"{self.signal.symbol} {self.side.value} @ {self.entry_price:.2f} "
            f"stop={self.stop_price:.2f} target={self.target_price:.2f} "
            f"rr={self.reward_risk:.2f} qty={self.quantity}"
        )


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        raise ValueError("tick must be positive")
    return round(float(price) / float(tick)) * float(tick)


def _validate(sides: str, session: str) -> tuple[str, str]:
    normalized_sides = sides.strip().lower()
    if normalized_sides not in SIDES_CHOICES:
        raise ValueError(f"sides must be one of {', '.join(SIDES_CHOICES)}")
    normalized_session = session.strip().lower()
    if normalized_session not in SESSION_CHOICES:
        raise ValueError(f"session must be one of {', '.join(SESSION_CHOICES)}")
    return normalized_sides, normalized_session


def update_channels(
    channels: list[Channel],
    *,
    index: int,
    close: float,
    channel_event: bool,
    top: float,
    bottom: float,
    bar_time: datetime,
    profile: Instrument,
    overlap: bool,
    breakout_ticks: float,
    max_channels: int = 500,
    remove_one_breakout: bool = False,
) -> tuple[list[Channel], bool]:
    """Apply the close-time channel lifecycle and return ``(channels, blocked)``.

    Channels are ordered newest first.  Broken channels are removed before the
    new channel is considered, matching Pine's close-based breakout test.  With
    ``overlap=False`` a new channel is rejected when it overlaps any active one.

    ``remove_one_breakout`` reproduces the source loop, which removes the first
    broken box and immediately breaks out of the loop, leaving any older broken
    boxes in place.  The default removes every broken channel.
    """
    blocked = False
    if remove_one_breakout:
        kept = list(channels)
        for offset, channel in enumerate(channels):
            if channel.is_broken(close, profile.tick_size, breakout_ticks):
                kept = channels[:offset] + channels[offset + 1 :]
                blocked = True
                break
    else:
        kept = [
            channel
            for channel in channels
            if not channel.is_broken(close, profile.tick_size, breakout_ticks)
        ]
        blocked = len(kept) != len(channels)
    if channel_event and math.isfinite(top) and math.isfinite(bottom):
        width = float(top) - float(bottom)
        if width > 0:
            candidates = [
                Channel(
                    top=float(top),
                    bottom=float(bottom),
                    created_index=index,
                    created_time=bar_time,
                    width=width,
                )
            ]
            if not overlap:
                candidates = [
                    candidate
                    for candidate in candidates
                    if not any(
                        candidate.top > existing.bottom and candidate.bottom < existing.top
                        for existing in kept
                    )
                ]
            kept = candidates + kept
    return kept[:max_channels], blocked


def evaluate_signal(
    frame: pd.DataFrame,
    index: int,
    channels: list[Channel],
    profile: Instrument,
    *,
    sides: str = "both",
    entry_zone: float = 0.10,
    min_channel_pct: float | None = None,
    atr_filter: bool = True,
    atr_min: float = 4.0,
    atr_percentile: float | None = None,
    cooldown_bars: int = 8,
    last_long_signal: int = -10**9,
    last_short_signal: int = -10**9,
    session: str = "all",
    skip_hours: tuple[int, ...] = (),
    event_codes: tuple[str, ...] = (),
    calendar: pd.DataFrame | None = None,
    allow_creation_entry: bool = True,
    require_inside: bool = False,
    ambiguous: str = "long",
) -> Evaluation:
    """Evaluate one completed bar against the active channels."""
    sides, session = _validate(sides, session)
    if not 0 <= index < len(frame):
        raise IndexError("index is outside the frame")
    row = frame.iloc[index]
    bar_time = pd.Timestamp(frame.index[index])
    if bar_time.tzinfo is None:
        bar_time = bar_time.tz_localize(NY_TZ)
    else:
        bar_time = bar_time.tz_convert(NY_TZ)

    if not session_allows(bar_time, session):
        return Evaluation(reason="session")
    if not bool(skip_hours_mask(pd.DatetimeIndex([bar_time]), skip_hours)[0]):
        return Evaluation(reason="hour")
    if event_codes and calendar is not None and event_on(bar_time.date(), calendar, event_codes):
        return Evaluation(reason="event")
    if not channels:
        return Evaluation(reason="no_channel")

    atr = float(row.get("atr_1m", row.get("atr", 0.0)))
    if not math.isfinite(atr):
        return Evaluation(reason="no_atr")
    if atr_filter:
        if atr_percentile is not None:
            # A percentile threshold is supplied by the caller as a dollar
            # value; this keeps the live and research paths identical.
            threshold = float(atr_percentile)
        else:
            threshold = float(atr_min)
        if atr < threshold:
            return Evaluation(reason="atr")

    low = float(row["low"])
    high = float(row["high"])
    close = float(row["close"])

    long_allowed = sides in {"long", "both"} and index - last_long_signal >= cooldown_bars
    short_allowed = sides in {"short", "both"} and index - last_short_signal >= cooldown_bars
    if not long_allowed and not short_allowed:
        return Evaluation(reason="cooldown")

    long_hit: tuple[Channel, float] | None = None
    short_hit: tuple[Channel, float] | None = None
    for channel in channels:
        if channel.width <= 0:
            continue
        if min_channel_pct is not None and channel.width < close * min_channel_pct:
            continue
        if not allow_creation_entry and channel.created_index == index:
            continue
        if require_inside and not (channel.bottom <= close <= channel.top):
            continue
        long_zone = channel.bottom + channel.width * entry_zone
        short_zone = channel.top - channel.width * entry_zone
        if long_allowed and long_hit is None and low <= long_zone:
            long_hit = (channel, long_zone)
        if short_allowed and short_hit is None and high >= short_zone:
            short_hit = (channel, short_zone)
        if long_hit is not None and short_hit is not None:
            break

    if long_hit is None and short_hit is None:
        return Evaluation(reason="no_touch")
    if long_hit is not None and short_hit is not None:
        if ambiguous == "skip":
            return Evaluation(reason="ambiguous")
        if ambiguous not in {"long", "short"}:
            raise ValueError("ambiguous must be long, short or skip")
        chosen = "long" if ambiguous == "long" else "short"
    else:
        chosen = "long" if long_hit is not None else "short"

    if chosen == "long":
        channel, zone = long_hit  # type: ignore[misc]
        side = Side.LONG
    else:
        channel, zone = short_hit  # type: ignore[misc]
        side = Side.SHORT

    signal = Signal(
        symbol=profile.symbol,
        side=side,
        bar_time=bar_time.to_pydatetime(),
        signal_time=(bar_time + pd.Timedelta(minutes=_bar_minutes(frame, index))).to_pydatetime(),
        channel=channel,
        zone_price=float(zone),
        low=low,
        high=high,
        close=close,
        atr=float(atr),
    )
    return Evaluation(signal=signal)


def _bar_minutes(frame: pd.DataFrame, index: int) -> int:
    if "bar_close" in frame.columns:
        value = frame.iloc[index]["bar_close"]
        if pd.notna(value):
            delta = pd.Timestamp(value) - pd.Timestamp(frame.index[index])
            return max(1, int(round(delta.total_seconds() / 60.0)))
    return 1


def plan_entry(
    signal: Signal,
    entry_price: float,
    profile: Instrument,
    *,
    quantity: int,
    stop_ticks: float,
    tp_mode: str = "channel",
    tp_channel_pct: float = 0.40,
    tp_fixed_ticks: float = 30.0,
) -> EntryPlan:
    """Build the channel bracket from an actual or reference entry price."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if stop_ticks < 0:
        raise ValueError("stop_ticks must be non-negative")
    if tp_mode not in {"channel", "fixed"}:
        raise ValueError("tp_mode must be channel or fixed")
    tick = profile.tick_size
    channel = signal.channel
    width = channel.width
    if width <= 0:
        raise ValueError("channel width must be positive")

    entry = round_to_tick(entry_price, tick)
    if signal.side is Side.LONG:
        stop = round_to_tick(channel.bottom - stop_ticks * tick, tick)
        distance = width * tp_channel_pct if tp_mode == "channel" else tp_fixed_ticks * tick
        target = round_to_tick(entry + distance, tick)
        if not stop < entry < target:
            raise ValueError(
                f"long bracket invalid: stop={stop:.4f} entry={entry:.4f} target={target:.4f}"
            )
    else:
        stop = round_to_tick(channel.top + stop_ticks * tick, tick)
        distance = width * tp_channel_pct if tp_mode == "channel" else tp_fixed_ticks * tick
        target = round_to_tick(entry - distance, tick)
        if not target < entry < stop:
            raise ValueError(
                f"short bracket invalid: target={target:.4f} entry={entry:.4f} stop={stop:.4f}"
            )

    return EntryPlan(
        signal=signal,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=quantity,
        point_value=profile.point_value,
        tick_size=tick,
        channel_width=width,
    )


def evaluate_last(
    frame: pd.DataFrame,
    channels: list[Channel],
    profile: Instrument,
    **kwargs: object,
) -> Evaluation:
    """Evaluate the last completed bar (live helper)."""
    if frame.empty:
        return Evaluation(reason="empty")
    return evaluate_signal(frame, len(frame) - 1, channels, profile, **kwargs)  # type: ignore[arg-type]
