"""Offline tests for the SkyQ RANGE channel scalper."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from config.settings import NY_TZ, instrument
from src.backtest import run_backtest
from src.indicators import add_features, load_bars, normalise_bars
from src.main import _bar_minutes, _bar_size, _plan_valid
from src.order_manager import build_bracket
from src.strategy_logic import (
    Channel,
    Side,
    evaluate_signal,
    plan_entry,
    update_channels,
)

SPY = instrument("SPY")


def _bars(
    prices: list[float],
    *,
    start: str = "2026-01-05 09:30",
    freq: str = "1min",
    high: float | None = None,
    low: float | None = None,
) -> pd.DataFrame:
    index = pd.date_range(start, periods=len(prices), freq=freq, tz=NY_TZ)
    return pd.DataFrame(
        {
            "open": prices,
            "high": [high if high is not None else price + 0.5 for price in prices],
            "low": [low if low is not None else price - 0.5 for price in prices],
            "close": prices,
            "volume": [1000.0] * len(prices),
        },
        index=index,
    )


def _feature_frame(
    *,
    event: bool = True,
    top: float = 101.0,
    bottom: float = 99.0,
    periods: int = 6,
    start: str = "2026-01-05 09:30",
) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="1min", tz=NY_TZ)
    frame = pd.DataFrame(
        {
            "open": [100.0] * periods,
            "high": [100.5] * periods,
            "low": [99.5] * periods,
            "close": [100.0] * periods,
            "volume": [1000.0] * periods,
            "channel_event": [event] + [False] * (periods - 1),
            "channel_top": [top] + [np.nan] * (periods - 1),
            "channel_bottom": [bottom] + [np.nan] * (periods - 1),
            "atr": [1.0] * periods,
            "atr_1m": [1.0] * periods,
            "bar_close": index + pd.Timedelta(minutes=1),
        },
        index=index,
    )
    return frame


class FeatureTests(unittest.TestCase):
    def test_normalise_localises_and_sorts(self) -> None:
        index = pd.to_datetime(["2026-01-05 10:00", "2026-01-05 09:30"]).tz_localize(NY_TZ)
        frame = pd.DataFrame(
            {
                "open": [2.0, 1.0],
                "high": [3.0, 2.0],
                "low": [1.0, 0.5],
                "close": [2.5, 1.5],
                "volume": [10.0, 20.0],
            },
            index=index,
        )
        cleaned = normalise_bars(frame)
        self.assertEqual(list(cleaned.index), sorted(cleaned.index))
        self.assertEqual(cleaned.index.tz, NY_TZ)

    def test_csv_loader_parses_utc_and_keeps_minutes(self) -> None:
        rows = ["Timestamp,Open,High,Low,Close,TickVolume,Spread"]
        for minute in range(10):
            price = 100.0 + minute
            rows.append(
                f"2026-01-05 14:{30 + minute}:00,{price},{price + 1},"
                f"{price - 1},{price + 0.5},10,2"
            )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "US500m_PERIOD_M1_OHLC.csv"
            path.write_text("\n".join(rows), encoding="utf-8")
            frame = load_bars(str(path), timeframe="1m")

        self.assertEqual(len(frame), 10)
        self.assertEqual(frame.index.tz, NY_TZ)
        self.assertEqual((frame.index[0].hour, frame.index[0].minute), (9, 30))
        self.assertAlmostEqual(float(frame.iloc[0]["open"]), 100.0)
        self.assertAlmostEqual(float(frame.iloc[0]["volume"]), 10.0)

    def test_features_are_causal(self) -> None:
        rng = np.random.default_rng(7)
        prices = 100 + np.cumsum(rng.normal(0, 0.15, 260))
        frame = _bars(list(prices))
        full = add_features(frame, timeframe="1m")
        prefix = add_features(frame.iloc[:200], timeframe="1m")
        for column in ("norm", "vol", "upper", "lower", "duration", "channel_event", "channel_top", "channel_bottom"):
            pd.testing.assert_series_equal(
                full[column].iloc[:200],
                prefix[column],
                check_names=False,
                check_exact=False,
                rtol=1e-12,
                atol=1e-12,
            )

    def test_channel_event_is_boolean_and_top_above_bottom(self) -> None:
        rng = np.random.default_rng(3)
        prices = 100 + np.cumsum(rng.normal(0, 0.2, 500))
        frame = add_features(_bars(list(prices)), timeframe="1m")
        self.assertTrue(frame["channel_event"].dtype == bool)
        events = frame[frame["channel_event"]]
        self.assertGreater(len(events), 0)
        self.assertTrue((events["channel_top"] > events["channel_bottom"]).all())


class ChannelLifecycleTests(unittest.TestCase):
    def test_add_and_breakout(self) -> None:
        channel = Channel(101.0, 99.0, 0, pd.Timestamp("2026-01-05 09:30", tz=NY_TZ), 2.0)
        channels, blocked = update_channels(
            [],
            index=1,
            close=100.0,
            channel_event=True,
            top=101.0,
            bottom=99.0,
            bar_time=pd.Timestamp("2026-01-05 09:31", tz=NY_TZ).to_pydatetime(),
            profile=SPY,
            overlap=True,
            breakout_ticks=10.0,
        )
        self.assertFalse(blocked)
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].width, 2.0)

        channels, blocked = update_channels(
            channels,
            index=2,
            close=102.0,
            channel_event=False,
            top=np.nan,
            bottom=np.nan,
            bar_time=pd.Timestamp("2026-01-05 09:32", tz=NY_TZ).to_pydatetime(),
            profile=SPY,
            overlap=True,
            breakout_ticks=0.0,
        )
        self.assertTrue(blocked)
        self.assertEqual(channels, [])
        self.assertLess(channel.bottom, channel.top)

    def test_overlap_filter(self) -> None:
        existing = Channel(101.0, 99.0, 0, pd.Timestamp("2026-01-05 09:30", tz=NY_TZ), 2.0)
        channels, _ = update_channels(
            [existing],
            index=1,
            close=100.0,
            channel_event=True,
            top=100.5,
            bottom=99.5,
            bar_time=pd.Timestamp("2026-01-05 09:31", tz=NY_TZ).to_pydatetime(),
            profile=SPY,
            overlap=False,
            breakout_ticks=10.0,
        )
        self.assertEqual(len(channels), 1)
        self.assertIs(channels[0], existing)

    def test_remove_one_breakout_matches_source_loop(self) -> None:
        first = Channel(101.0, 99.0, 0, pd.Timestamp("2026-01-05 09:30", tz=NY_TZ), 2.0)
        second = Channel(105.0, 103.0, 1, pd.Timestamp("2026-01-05 09:31", tz=NY_TZ), 2.0)
        channels, blocked = update_channels(
            [first, second],
            index=2,
            close=102.0,
            channel_event=False,
            top=np.nan,
            bottom=np.nan,
            bar_time=pd.Timestamp("2026-01-05 09:32", tz=NY_TZ).to_pydatetime(),
            profile=SPY,
            overlap=True,
            breakout_ticks=0.0,
            remove_one_breakout=True,
        )
        self.assertTrue(blocked)
        self.assertEqual(channels, [second])


class SignalTests(unittest.TestCase):
    def _frame(self, *, start: str = "2026-01-05 09:30", low: float = 99.1, high: float = 100.9) -> pd.DataFrame:
        frame = _feature_frame(start=start)
        frame.loc[frame.index[0], "low"] = low
        frame.loc[frame.index[0], "high"] = high
        frame.loc[frame.index[0], "close"] = 100.0
        return frame

    def _channel(self, *, created_index: int = 0) -> Channel:
        return Channel(
            101.0,
            99.0,
            created_index,
            pd.Timestamp("2026-01-05 09:30", tz=NY_TZ).to_pydatetime(),
            2.0,
        )

    def test_long_touch(self) -> None:
        frame = self._frame(low=99.1, high=100.0)
        result = evaluate_signal(
            frame, 0, [self._channel()], SPY, sides="long", entry_zone=0.1,
            atr_filter=False, cooldown_bars=0, session="rth",
        )
        self.assertIsNotNone(result.signal)
        self.assertIs(result.signal.side, Side.LONG)
        self.assertAlmostEqual(result.signal.zone_price, 99.2)

    def test_short_touch(self) -> None:
        frame = self._frame(low=100.0, high=100.9)
        result = evaluate_signal(
            frame, 0, [self._channel()], SPY, sides="short", entry_zone=0.1,
            atr_filter=False, cooldown_bars=0, session="rth",
        )
        self.assertIsNotNone(result.signal)
        self.assertIs(result.signal.side, Side.SHORT)
        self.assertAlmostEqual(result.signal.zone_price, 100.8)

    def test_ambiguous_defaults_long_and_can_skip(self) -> None:
        frame = self._frame(low=99.1, high=100.9)
        long_result = evaluate_signal(
            frame, 0, [self._channel()], SPY, sides="both", entry_zone=0.1,
            atr_filter=False, cooldown_bars=0, session="rth", ambiguous="long",
        )
        self.assertIs(long_result.signal.side, Side.LONG)
        skipped = evaluate_signal(
            frame, 0, [self._channel()], SPY, sides="both", entry_zone=0.1,
            atr_filter=False, cooldown_bars=0, session="rth", ambiguous="skip",
        )
        self.assertIsNone(skipped.signal)
        self.assertEqual(skipped.reason, "ambiguous")

    def test_session_and_cooldown_gates(self) -> None:
        frame = self._frame(start="2026-01-05 08:00")
        outside = evaluate_signal(
            frame, 0, [self._channel()], SPY, sides="long", entry_zone=0.1,
            atr_filter=False, cooldown_bars=0, session="rth",
        )
        self.assertIsNone(outside.signal)
        self.assertEqual(outside.reason, "session")

        frame = self._frame()
        cooling = evaluate_signal(
            frame, 0, [self._channel()], SPY, sides="long", entry_zone=0.1,
            atr_filter=False, cooldown_bars=8, last_long_signal=0, session="rth",
        )
        self.assertIsNone(cooling.signal)
        self.assertEqual(cooling.reason, "cooldown")

    def test_atr_filter_and_creation_entry(self) -> None:
        frame = self._frame(low=99.1)
        blocked = evaluate_signal(
            frame, 0, [self._channel()], SPY, sides="long", entry_zone=0.1,
            atr_filter=True, atr_min=2.0, cooldown_bars=0, session="rth",
        )
        self.assertIsNone(blocked.signal)
        self.assertEqual(blocked.reason, "atr")

        no_creation = evaluate_signal(
            frame, 0, [self._channel(created_index=0)], SPY, sides="long",
            entry_zone=0.1, atr_filter=False, cooldown_bars=0, session="rth",
            allow_creation_entry=False,
        )
        self.assertIsNone(no_creation.signal)
        self.assertEqual(no_creation.reason, "no_touch")


class BracketTests(unittest.TestCase):
    def _signal(self, side: Side = Side.LONG):
        frame = _feature_frame()
        frame.loc[frame.index[0], "low"] = 99.1
        frame.loc[frame.index[0], "high"] = 100.9
        result = evaluate_signal(
            frame, 0, [Channel(101.0, 99.0, 0, pd.Timestamp("2026-01-05 09:30", tz=NY_TZ).to_pydatetime(), 2.0)],
            SPY, sides="both", entry_zone=0.1, atr_filter=False, cooldown_bars=0,
            session="rth", ambiguous=side.value.lower(),
        )
        return result.signal

    def test_long_and_short_geometry(self) -> None:
        long_signal = self._signal(Side.LONG)
        plan = plan_entry(long_signal, 100.0, SPY, quantity=25, stop_ticks=50, tp_channel_pct=0.4)
        self.assertLess(plan.stop_price, plan.entry_price)
        self.assertGreater(plan.target_price, plan.entry_price)
        self.assertAlmostEqual(plan.stop_distance, 1.5)
        self.assertAlmostEqual(plan.target_distance, 0.8)
        self.assertEqual(plan.quantity, 25)

        short_signal = self._signal(Side.SHORT)
        plan = plan_entry(short_signal, 100.0, SPY, quantity=25, stop_ticks=50, tp_channel_pct=0.4)
        self.assertGreater(plan.stop_price, plan.entry_price)
        self.assertLess(plan.target_price, plan.entry_price)

    def test_invalid_bracket_raises(self) -> None:
        signal = self._signal(Side.LONG)
        with self.assertRaises(ValueError):
            plan_entry(signal, 100.0, SPY, quantity=1, stop_ticks=50, tp_channel_pct=0.0)

    def test_build_bracket_for_stock(self) -> None:
        counter = iter(range(100, 103))
        bracket = build_bracket(SPY, Side.LONG, 25, 100.0, 99.5, 100.5, lambda: next(counter))
        self.assertEqual(bracket.parent.totalQuantity, 25)
        self.assertEqual(bracket.take_profit.lmtPrice, 100.5)
        self.assertEqual(bracket.stop_loss.auxPrice, 99.5)
        self.assertTrue(bracket.oca_group.startswith("SKYQ-RANGE-"))
        self.assertEqual(bracket.take_profit.ocaGroup, bracket.stop_loss.ocaGroup)


class BacktestTests(unittest.TestCase):
    def _run(self, frame: pd.DataFrame, **kwargs):
        defaults = dict(
            timeframe="1m",
            entry_mode="next_open",
            sides="long",
            quantity=100,
            entry_zone=0.1,
            min_channel_width=0.0,
            stop_ticks=50,
            tp_channel_pct=0.4,
            cooldown_bars=0,
            overlap=True,
            atr_filter=False,
            session="rth",
            commission_per_contract=0,
            entry_slippage_ticks=0.0,
            exit_slippage_ticks=0.0,
            assume_stop_first=True,
        )
        defaults.update(kwargs)
        return run_backtest(frame, SPY, **defaults)

    def test_target_hit_next_bar(self) -> None:
        frame = _feature_frame()
        frame.loc[frame.index[0], "low"] = 99.1
        frame.loc[frame.index[1], "open"] = 100.0
        frame.loc[frame.index[1], "high"] = 101.0
        frame.loc[frame.index[1], "low"] = 100.0
        result = self._run(frame)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades[0]
        self.assertEqual(trade.exit_reason, "TARGET")
        self.assertAlmostEqual(trade.entry_price, 100.0)
        self.assertAlmostEqual(trade.exit_price, 100.8)
        # 100 shares at $0.005/share hits the $1/order minimum on both sides.
        self.assertAlmostEqual(trade.net_pnl, 78.0)

    def test_stop_hit_and_stop_first(self) -> None:
        frame = _feature_frame()
        frame.loc[frame.index[0], "low"] = 99.1
        frame.loc[frame.index[1], "open"] = 100.0
        frame.loc[frame.index[1], "high"] = 101.0
        frame.loc[frame.index[1], "low"] = 98.0
        result = self._run(frame, cooldown_bars=10)
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "STOP")
        self.assertAlmostEqual(result.trades[0].net_pnl, -152.0)

    def test_zone_limit_does_not_fill_without_a_retest(self) -> None:
        frame = _feature_frame()
        frame.loc[frame.index[0], "low"] = 99.1
        frame.loc[frame.index[1], "open"] = 100.0
        frame.loc[frame.index[1], "low"] = 99.5
        frame.loc[frame.index[1], "high"] = 100.2
        result = self._run(frame, entry_mode="zone")
        self.assertEqual(result.trades, [])
        self.assertGreaterEqual(result.gate_counts.get("no_fill", 0), 1)

    def test_zone_entry_bar_target_needs_a_close_beyond_target(self) -> None:
        frame = _feature_frame()
        frame.loc[frame.index[0], "low"] = 99.1
        frame.loc[frame.index[1], "open"] = 100.0
        frame.loc[frame.index[1], "low"] = 99.0
        frame.loc[frame.index[1], "high"] = 101.0
        frame.loc[frame.index[1], "close"] = 99.9
        frame.loc[frame.index[2]:, "open"] = 99.8
        frame.loc[frame.index[2]:, "high"] = 99.95
        frame.loc[frame.index[2]:, "low"] = 99.5
        frame.loc[frame.index[2]:, "close"] = 99.8
        result = self._run(frame, entry_mode="zone")
        # The entry bar cannot credit the target because it closed below it;
        # later bars have no target touch, so the position ends at the last close.
        self.assertEqual(len(result.trades), 1)
        self.assertEqual(result.trades[0].exit_reason, "END")

    def test_max_hold_and_flat(self) -> None:
        frame = _feature_frame(periods=8)
        frame.loc[frame.index[0], "low"] = 99.1
        frame.loc[frame.index[1]:, "open"] = 100.0
        frame.loc[frame.index[1]:, "high"] = 100.2
        frame.loc[frame.index[1]:, "low"] = 99.9
        frame.loc[frame.index[1]:, "close"] = 100.0
        result = self._run(frame, max_hold_bars=2)
        self.assertEqual(result.trades[0].exit_reason, "MAX_HOLD")

        result = self._run(frame, flat_at_minute=9 * 60 + 31)
        self.assertEqual(result.trades[0].exit_reason, "FLAT")

    def test_no_signal_when_channel_is_too_narrow(self) -> None:
        frame = _feature_frame(top=100.2, bottom=99.8)
        frame.loc[frame.index[0], "low"] = 99.81
        result = self._run(frame, min_channel_width=0.5)
        self.assertEqual(result.trades, [])

    def test_price_relative_width_filter(self) -> None:
        frame = _feature_frame(top=100.2, bottom=99.8)
        frame.loc[frame.index[0], "low"] = 99.81
        blocked = self._run(frame, min_channel_width=0.0, min_channel_pct=0.01)
        self.assertEqual(blocked.trades, [])
        allowed = self._run(frame, min_channel_width=0.0, min_channel_pct=0.001, cooldown_bars=100)
        self.assertEqual(len(allowed.trades), 1)

    def test_event_day_filter_uses_dates(self) -> None:
        frame = _feature_frame()
        frame.loc[frame.index[0], "low"] = 99.1
        calendar = pd.DataFrame({"nfp": [1]}, index=[pd.Timestamp("2026-01-05").date()])
        allowed = self._run(frame, event_codes=(), calendar=calendar, cooldown_bars=100)
        self.assertEqual(len(allowed.trades), 1)
        blocked = self._run(frame, event_codes=("nfp",), calendar=calendar, cooldown_bars=100)
        self.assertEqual(blocked.trades, [])


class RunnerTests(unittest.TestCase):
    def test_bar_helpers(self) -> None:
        self.assertEqual(_bar_minutes("1m"), 1)
        self.assertEqual(_bar_minutes("15m"), 15)
        self.assertEqual(_bar_size("5m"), "5 mins")

    def test_plan_valid(self) -> None:
        frame = _feature_frame()
        frame.loc[frame.index[0], "low"] = 99.1
        signal = evaluate_signal(
            frame, 0, [Channel(101.0, 99.0, 0, pd.Timestamp("2026-01-05 09:30", tz=NY_TZ).to_pydatetime(), 2.0)],
            SPY, sides="long", entry_zone=0.1, atr_filter=False, cooldown_bars=0, session="rth",
        ).signal
        plan = plan_entry(signal, 100.0, SPY, quantity=25, stop_ticks=50, tp_channel_pct=0.4)
        self.assertTrue(_plan_valid(plan, 100.0))
        self.assertFalse(_plan_valid(plan, 98.0))


if __name__ == "__main__":
    unittest.main()
