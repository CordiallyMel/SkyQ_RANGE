"""Deterministic paper-session engine for the RANGE strategy.

This is intentionally broker-neutral: it turns completed one-minute bars into
signals, fills a paper position at the next bar open, and applies the same
tick/commission assumptions as the research backtest.  A broker adapter can
mirror the emitted orders later without changing the signal path.

The engine is also a useful offline forward-test harness.  Feed it synthetic or
historical bars and it produces the same journal shape as the live runner.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from typing import Any

import pandas as pd

from config.settings import (
    ATR_FILTER,
    BREAKOUT_TICKS,
    NY_TZ,
    SESSION,
    SLIPPAGE_TICKS,
    BookPreset,
    Instrument,
    book_preset,
    instrument,
)
from src.indicators import add_features, gate_ready, normalise_bars
from src.strategy_logic import (
    EntryPlan,
    Side,
    Signal,
    evaluate_last,
    plan_entry,
    update_channels,
)

log = logging.getLogger("skyq.range.paper")

WINDOW_BARS = 1200
DEFAULT_QUANTITY = 1000
DEFAULT_FLAT_AT = time(15, 55)


@dataclass(frozen=True)
class RunnerSettings:
    """Per-runner knobs.  Every value is exposed in the JSON config."""

    runner_id: str
    symbol: str
    quantity: int = DEFAULT_QUANTITY
    sides: str = "both"
    stop_ticks: float | None = None
    max_hold_bars: int | None = None
    entry_zone: float = 0.20
    min_channel_pct: float | None = None
    tp_channel_pct: float = 0.20
    atr_filter: bool = ATR_FILTER
    atr_min: float = 4.0
    session: str = SESSION
    flat_at: str | None = "15:55"
    slippage_ticks: float = SLIPPAGE_TICKS
    broker: str = "simulated"
    data_source: str = "lse-shared-1m"
    poll_seconds: float = 5.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunnerSettings":
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unknown runner keys: {', '.join(sorted(unknown))}")
        return cls(**payload)

    def flat_time(self) -> time | None:
        if not self.flat_at:
            return None
        hour, minute = self.flat_at.split(":", 1)
        return time(int(hour), int(minute))


@dataclass
class PaperPosition:
    plan: EntryPlan
    entry_time: datetime
    bars_held: int = 0

    @property
    def side(self) -> Side:
        return self.plan.side


@dataclass
class PaperTrade:
    runner_id: str
    symbol: str
    side: str
    signal_time: datetime
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    exit_time: datetime
    exit_price: float
    exit_reason: str
    quantity: int
    bars_held: int
    gross_pnl: float
    costs: float
    net_pnl: float
    channel_width: float
    zone_price: float
    atr: float

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("signal_time", "entry_time", "exit_time"):
            value = payload[key]
            payload[key] = value.isoformat() if isinstance(value, datetime) else value
        return payload


@dataclass
class PaperMetrics:
    trades: int = 0
    gross_pnl: float = 0.0
    costs: float = 0.0
    net_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    flat: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    max_drawdown: float = 0.0
    equity_peak: float = 0.0
    avg_trade: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = None

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


@dataclass
class PaperSession:
    """One independent paper book."""

    settings: RunnerSettings
    profile: Instrument
    preset: BookPreset
    trades: list[PaperTrade] = field(default_factory=list)
    metrics: PaperMetrics = field(default_factory=PaperMetrics)
    position: PaperPosition | None = None
    pending_signal: Signal | None = None
    last_price: float | None = None
    last_bar_time: datetime | None = None
    skipped: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._bars = pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"], dtype=float
        )
        self._channels = []
        self._seeded = False
        self._slippage = max(0.0, float(self.settings.slippage_ticks))
        self._flat_at = self.settings.flat_time()
        self._stop_ticks = (
            float(self.settings.stop_ticks)
            if self.settings.stop_ticks is not None
            else float(self.preset.stop_ticks)
        )
        self._max_hold = (
            int(self.settings.max_hold_bars)
            if self.settings.max_hold_bars is not None
            else int(self.preset.max_hold_bars)
        )
        self._min_channel_pct = (
            float(self.settings.min_channel_pct)
            if self.settings.min_channel_pct is not None
            else float(self.preset.min_channel_pct)
        )

    @classmethod
    def from_config(
        cls, payload: dict[str, Any], *, profile: Instrument | None = None
    ) -> "PaperSession":
        settings = RunnerSettings.from_dict(payload)
        selected = profile or instrument(settings.symbol)
        if selected.symbol not in ("SPY", "QQQ"):
            raise ValueError(
                f"{selected.symbol} is research-only. The deployable RANGE books "
                "are SPY and QQQ; see reports/futures_gate.csv."
            )
        preset = book_preset(selected.symbol)
        if settings.quantity <= 0:
            raise ValueError("quantity must be positive")
        if settings.sides not in {"both", "long", "short"}:
            raise ValueError("sides must be both, long or short")
        return cls(settings=settings, profile=selected, preset=preset)

    @property
    def is_flat(self) -> bool:
        return self.position is None

    @property
    def bars(self) -> pd.DataFrame:
        return self._bars

    def seed(self, frame: pd.DataFrame) -> None:
        """Load historical completed bars and build channels without trading."""
        data = normalise_bars(frame)
        if data.empty:
            return
        self._bars = data.tail(5000).copy()
        self._channels = []
        features = gate_ready(add_features(self._bars, timeframe="1m"))
        for index in range(len(features)):
            row = features.iloc[index]
            self._channels, _ = update_channels(
                self._channels,
                index=index,
                close=float(row["close"]),
                channel_event=bool(row["channel_event"]),
                top=float(row["channel_top"]),
                bottom=float(row["channel_bottom"]),
                bar_time=pd.Timestamp(features.index[index]).to_pydatetime(),
                profile=self.profile,
                overlap=True,
                breakout_ticks=BREAKOUT_TICKS,
            )
        self._seeded = True
        self.last_bar_time = pd.Timestamp(self._bars.index[-1]).to_pydatetime()
        self.last_price = float(self._bars["close"].iloc[-1])
        log.info(
            "seeded %s with %d bars and %d active channels",
            self.settings.runner_id,
            len(self._bars),
            len(self._channels),
        )

    def sync(self, frame: pd.DataFrame) -> list[PaperTrade]:
        """Consume newly completed rows from a shared-source frame."""
        data = normalise_bars(frame)
        if data.empty:
            return []
        if not self._seeded:
            self.seed(data)
            return []
        known = self._bars.index[-1] if not self._bars.empty else None
        new_rows = data.loc[data.index > known] if known is not None else data
        opened: list[PaperTrade] = []
        for timestamp, row in new_rows.iterrows():
            before = len(self.trades)
            self.on_bar(
                pd.Timestamp(timestamp),
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row.get("volume", 0.0)),
            )
            if len(self.trades) > before:
                opened.extend(self.trades[before:])
        return opened

    def on_bar(
        self,
        timestamp: pd.Timestamp | datetime,
        open_price: float,
        high: float,
        low: float,
        close: float,
        volume: float = 0.0,
    ) -> PaperTrade | None:
        stamp = pd.Timestamp(timestamp)
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(NY_TZ)
        else:
            stamp = stamp.tz_convert(NY_TZ)
        if self.last_bar_time is not None and stamp <= pd.Timestamp(self.last_bar_time):
            return None

        row = pd.DataFrame(
            {
                "open": [float(open_price)],
                "high": [float(high)],
                "low": [float(low)],
                "close": [float(close)],
                "volume": [float(volume)],
            },
            index=pd.DatetimeIndex([stamp]),
        )
        self._bars = pd.concat([self._bars, row]).tail(5000)
        self.last_bar_time = stamp.to_pydatetime()
        self.last_price = float(close)
        completed_trade: PaperTrade | None = None

        # A signal is emitted only after a bar closes, so the next bar's open
        # is the earliest honest fill.  This also avoids a same-bar lookahead.
        after_flat = self._flat_at is not None and stamp.time() >= self._flat_at
        if self.pending_signal is not None:
            if after_flat:
                self.pending_signal = None
            else:
                completed_trade = self._try_fill(stamp, float(open_price))
                self.pending_signal = None

        if self.position is not None:
            exited = self._manage_position(stamp, high, low, close)
            if exited is not None:
                completed_trade = exited

        # The channel lifecycle advances on every completed bar, including a
        # bar where a position is open and bars after the flat time.  This is
        # what the backtest does; freezing channels during a hold would change
        # the next trade's channel state.
        features = self._update_channels()
        if after_flat:
            self.pending_signal = None
            return completed_trade

        if self.position is None and self.pending_signal is None:
            self._evaluate(features, stamp)
        return completed_trade

    def _update_channels(self) -> pd.DataFrame:
        window = self._bars.tail(WINDOW_BARS)
        features = gate_ready(add_features(window, timeframe="1m"))
        if features.empty:
            return features
        last = features.index[-1]
        row = features.iloc[-1]
        self._channels, _ = update_channels(
            self._channels,
            index=len(features) - 1,
            close=float(row["close"]),
            channel_event=bool(row["channel_event"]),
            top=float(row["channel_top"]),
            bottom=float(row["channel_bottom"]),
            bar_time=pd.Timestamp(last).to_pydatetime(),
            profile=self.profile,
            overlap=True,
            breakout_ticks=BREAKOUT_TICKS,
        )
        return features

    def _evaluate(self, features: pd.DataFrame, stamp: pd.Timestamp) -> None:
        if features.empty:
            return
        row = features.iloc[-1]
        evaluation = evaluate_last(
            features,
            self._channels,
            self.profile,
            sides=self.settings.sides,
            entry_zone=self.settings.entry_zone,
            min_channel_pct=self._min_channel_pct,
            atr_filter=self.settings.atr_filter,
            atr_min=self.settings.atr_min,
            cooldown_bars=0,
            session=self.settings.session,
            allow_creation_entry=True,
            require_inside=False,
            ambiguous="long",
        )
        if evaluation.signal is None:
            reason = evaluation.reason or "none"
            self.skipped[reason] = self.skipped.get(reason, 0) + 1
            return
        self.pending_signal = evaluation.signal
        log.info(
            "%s signal %s at %s close=%.2f",
            self.settings.runner_id,
            evaluation.signal.side.value,
            stamp,
            float(row["close"]),
        )

    def _try_fill(self, stamp: pd.Timestamp, bar_open: float) -> PaperTrade | None:
        signal = self.pending_signal
        if signal is None or self.position is not None:
            return None
        tick = self.profile.tick_size
        direction = signal.side.direction
        fill = bar_open + direction * self._slippage * tick
        try:
            plan = plan_entry(
                signal,
                fill,
                self.profile,
                quantity=self.settings.quantity,
                stop_ticks=self._stop_ticks,
                tp_mode="channel",
                tp_channel_pct=self.settings.tp_channel_pct,
            )
        except ValueError as exc:
            self.skipped["invalid_bracket"] = self.skipped.get("invalid_bracket", 0) + 1
            log.info("%s skipped fill: %s", self.settings.runner_id, exc)
            return None
        self.position = PaperPosition(plan=plan, entry_time=stamp.to_pydatetime())
        log.info(
            "%s PAPER ENTRY %s qty=%d @ %.2f stop=%.2f target=%.2f",
            self.settings.runner_id,
            plan.side.value,
            plan.quantity,
            plan.entry_price,
            plan.stop_price,
            plan.target_price,
        )
        return None

    def _manage_position(
        self, stamp: pd.Timestamp, high: float, low: float, close: float
    ) -> PaperTrade | None:
        position = self.position
        if position is None:
            return None
        position.bars_held += 1
        plan = position.plan
        side = plan.side
        tick = self.profile.tick_size
        stop = plan.stop_price
        target = plan.target_price
        exit_price: float | None = None
        reason: str | None = None

        # Conservative convention used by the research backtest: when a bar
        # contains both levels, assume the stop was hit first.
        if side is Side.LONG:
            if low <= stop:
                exit_price = stop - self._slippage * tick
                reason = "STOP"
            elif high >= target:
                exit_price = target
                reason = "TARGET"
        else:
            if high >= stop:
                exit_price = stop + self._slippage * tick
                reason = "STOP"
            elif low <= target:
                exit_price = target
                reason = "TARGET"

        if exit_price is None and position.bars_held >= self._max_hold:
            exit_price = close - side.direction * self._slippage * tick
            reason = "TIME"
        if (
            exit_price is None
            and self._flat_at is not None
            and stamp.time() >= self._flat_at
        ):
            exit_price = close - side.direction * self._slippage * tick
            reason = "FLAT"
        if exit_price is None or reason is None:
            return None
        return self._close_position(stamp, exit_price, reason)

    def _close_position(
        self, stamp: pd.Timestamp, exit_price: float, reason: str
    ) -> PaperTrade:
        assert self.position is not None
        position = self.position
        plan = position.plan
        direction = plan.side.direction
        gross = (
            (float(exit_price) - plan.entry_price)
            * direction
            * plan.quantity
            * self.profile.point_value
        )
        exit_slip = (
            0.0
            if reason == "TARGET"
            else self._slippage
            * self.profile.tick_size
            * plan.quantity
            * self.profile.point_value
        )
        commission = self._commission(plan.quantity)
        costs = exit_slip + commission
        trade = PaperTrade(
            runner_id=self.settings.runner_id,
            symbol=self.profile.symbol,
            side=plan.side.value,
            signal_time=plan.signal.bar_time,
            entry_time=position.entry_time,
            entry_price=plan.entry_price,
            stop_price=plan.stop_price,
            target_price=plan.target_price,
            exit_time=stamp.to_pydatetime(),
            exit_price=float(exit_price),
            exit_reason=reason,
            quantity=plan.quantity,
            bars_held=position.bars_held,
            gross_pnl=gross,
            costs=costs,
            net_pnl=gross - costs,
            channel_width=plan.channel_width,
            zone_price=plan.signal.zone_price,
            atr=plan.signal.atr,
        )
        self.trades.append(trade)
        self._update_metrics(trade)
        self.position = None
        log.info(
            "%s PAPER EXIT %s %s net=%.2f",
            self.settings.runner_id,
            reason,
            plan.side.value,
            trade.net_pnl,
        )
        return trade

    def _commission(self, quantity: int) -> float:
        if self.profile.is_future:
            per_contract = float(self.profile.commission_per_contract or 0.0)
            return per_contract * quantity * 2.0
        per_share = float(self.profile.commission_per_share or 0.0) * quantity * 2.0
        minimum = float(self.profile.min_commission_per_order or 0.0) * 2.0
        return max(minimum, per_share)

    def _update_metrics(self, trade: PaperTrade) -> None:
        metrics = self.metrics
        metrics.trades += 1
        metrics.gross_pnl += trade.gross_pnl
        metrics.costs += trade.costs
        metrics.net_pnl += trade.net_pnl
        if trade.net_pnl > 0:
            metrics.wins += 1
            metrics.gross_profit += trade.net_pnl
        elif trade.net_pnl < 0:
            metrics.losses += 1
            metrics.gross_loss += abs(trade.net_pnl)
        else:
            metrics.flat += 1
        metrics.equity_peak = max(metrics.equity_peak, metrics.net_pnl)
        metrics.max_drawdown = min(
            metrics.max_drawdown, metrics.net_pnl - metrics.equity_peak
        )
        metrics.avg_trade = metrics.net_pnl / metrics.trades
        metrics.win_rate = metrics.wins / metrics.trades * 100.0
        metrics.profit_factor = (
            metrics.gross_profit / metrics.gross_loss
            if metrics.gross_loss > 0
            else None
        )

    def force_flat(self, stamp: pd.Timestamp, price: float | None = None) -> PaperTrade | None:
        """Operator stop: close a position at the last known price."""
        if self.position is None:
            return None
        last = float(price if price is not None else self.last_price or 0.0)
        return self._close_position(pd.Timestamp(stamp), last, "MANUAL")

    def journal_frame(self) -> pd.DataFrame:
        return pd.DataFrame([trade.as_dict() for trade in self.trades])

    def snapshot(self) -> dict[str, Any]:
        position = None
        if self.position is not None:
            plan = self.position.plan
            position = {
                "side": plan.side.value,
                "quantity": plan.quantity,
                "entry_price": plan.entry_price,
                "stop_price": plan.stop_price,
                "target_price": plan.target_price,
                "entry_time": self.position.entry_time.isoformat(),
                "bars_held": self.position.bars_held,
            }
        return {
            "runner_id": self.settings.runner_id,
            "symbol": self.settings.symbol,
            "broker": self.settings.broker,
            "data_source": self.settings.data_source,
            "last_bar_time": self.last_bar_time.isoformat()
            if self.last_bar_time
            else None,
            "last_price": self.last_price,
            "position": position,
            "metrics": self.metrics.as_dict(),
            "skipped": dict(self.skipped),
        }


def settings_from_preset(
    runner_id: str, symbol: str, quantity: int = DEFAULT_QUANTITY, **overrides: Any
) -> RunnerSettings:
    preset = book_preset(symbol)
    payload = {
        "runner_id": runner_id,
        "symbol": symbol,
        "quantity": quantity,
        "stop_ticks": preset.stop_ticks,
        "max_hold_bars": preset.max_hold_bars,
        "min_channel_pct": preset.min_channel_pct,
        **overrides,
    }
    return RunnerSettings.from_dict(payload)


__all__ = [
    "DEFAULT_QUANTITY",
    "PaperMetrics",
    "PaperPosition",
    "PaperSession",
    "PaperTrade",
    "RunnerSettings",
    "settings_from_preset",
]
