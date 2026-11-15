"""Offline tests for the 1-minute source and paper-session engine."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from config.settings import NY_TZ, instrument
from integrations.lse_feed import FiveMinuteBarBuilder, LseBar, LseTick
from integrations.lse_one_minute import (
    LseOneMinuteSource,
    OneMinuteBarCache,
    SharedLseOneMinuteSource,
    one_minute_status_path,
)
from src.paper_session import PaperSession, RunnerSettings, settings_from_preset
from src.strategy_logic import Channel, Side, Signal


def _warmup_frame(rows: int = 300) -> pd.DataFrame:
    index = pd.date_range(
        "2026-01-02 09:30", periods=rows, freq="1min", tz=NY_TZ
    )
    return pd.DataFrame(
        {
            "open": [100.0] * rows,
            "high": [101.0] * rows,
            "low": [99.0] * rows,
            "close": [100.0] * rows,
            "volume": [1000.0] * rows,
        },
        index=index,
    )


class _FakeArchive:
    def bars(self, symbol: str, **kwargs: object) -> pd.DataFrame:
        assert kwargs.get("timeframe") == "1m"
        return _warmup_frame()


class _FakeFeed:
    def __init__(self, api_key: str, symbol: str, on_tick, on_disconnect=None):
        self.api_key = api_key
        self.symbol = symbol
        self.on_tick = on_tick
        self.on_disconnect = on_disconnect
        self.fatal = False
        self.ready = True
        self.last_tick: LseTick | None = None

    def start(self) -> None:
        tick = LseTick(
            symbol=self.symbol,
            price=100.25,
            volume=10.0,
            timestamp=datetime(2026, 1, 5, 9, 30, 10, tzinfo=NY_TZ),
            bid=100.24,
            ask=100.26,
        )
        self.last_tick = tick
        self.on_tick(tick)

    def wait_ready(self, timeout: float = 30.0) -> None:
        return None

    def status(self) -> str:
        return "fake feed ready"

    def stop(self) -> None:
        return None


class OneMinuteSourceTests(unittest.TestCase):
    def test_one_minute_builder_finalises_a_one_minute_bar(self) -> None:
        builder = FiveMinuteBarBuilder("SPY", minutes=1)
        builder.add_tick(
            LseTick(
                symbol="SPY",
                price=100.5,
                volume=4.0,
                timestamp=datetime(2026, 1, 5, 9, 30, 10, tzinfo=NY_TZ),
            )
        )
        finished = builder.finalize(
            datetime(2026, 1, 5, 9, 31, 10, tzinfo=NY_TZ), grace_seconds=5
        )
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].open, 100.5)
        self.assertEqual(finished[0].volume, 4.0)

    def test_source_downloads_1m_warmup_and_publishes_live_bar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = LseOneMinuteSource(
                "SPY",
                cache_dir=tmp,
                api_key="test-key",
                archive=_FakeArchive(),
                feed_factory=_FakeFeed,
            )
            source.start(
                now=datetime(2026, 1, 5, 9, 29, tzinfo=NY_TZ),
                timeout=2,
            )
            self.assertTrue(source._started)
            self.assertEqual(source._builder.minutes, 1)
            self.assertTrue(source.warmup_path.is_file())
            finished = source.poll(datetime(2026, 1, 5, 9, 31, 10, tzinfo=NY_TZ))
            self.assertEqual(len(finished), 1)
            frame = source.frame()
            self.assertEqual(len(frame), 301)
            self.assertEqual(frame.index[-1], pd.Timestamp("2026-01-05 09:30", tz=NY_TZ))
            payload = source.status_payload()
            self.assertEqual(payload["timeframe"], "1m")
            self.assertTrue(payload["ready"])
            self.assertTrue(payload["warmup_cache"].endswith(".parquet"))
            self.assertIsNotNone(payload["last_tick"])
            source.stop()


class SharedSourceTests(unittest.TestCase):
    def test_shared_source_reads_bridge_files_without_a_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            warmup = _warmup_frame(120)
            warmup_path = root / "SPY_1m_warmup.parquet"
            warmup.to_parquet(warmup_path)
            cache = OneMinuteBarCache(root / "live", "SPY")
            cache.append(
                LseBar(
                    time=pd.Timestamp("2026-01-05 09:30", tz=NY_TZ),
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=10.0,
                    trades=2,
                )
            )
            status_path = one_minute_status_path(root, "SPY")
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "symbol": "SPY",
                        "timeframe": "1m",
                        "session_date": "2026-01-05",
                        "ready": True,
                        "started_before_open": True,
                        "warmup_cache": str(warmup_path),
                        "updated_at": datetime.now(tz=NY_TZ).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
            source = SharedLseOneMinuteSource("SPY", cache_dir=root)
            source.start(
                now=datetime(2026, 1, 5, 10, 0, tzinfo=NY_TZ), timeout=2
            )
            frame = source.frame()
            self.assertEqual(len(frame), 121)
            self.assertEqual(frame.index[-1], pd.Timestamp("2026-01-05 09:30", tz=NY_TZ))
            self.assertTrue(source.status_payload()["ready"])
            source.stop()


class PaperSessionTests(unittest.TestCase):
    def _signal(self, side: Side = Side.LONG) -> Signal:
        channel = Channel(
            top=101.0,
            bottom=99.0,
            created_index=0,
            created_time=datetime(2026, 1, 5, 9, 30, tzinfo=NY_TZ),
            width=2.0,
        )
        return Signal(
            symbol="SPY",
            side=side,
            bar_time=datetime(2026, 1, 5, 9, 30, tzinfo=NY_TZ),
            signal_time=datetime(2026, 1, 5, 9, 31, tzinfo=NY_TZ),
            channel=channel,
            zone_price=99.4,
            low=99.0,
            high=101.0,
            close=100.0,
            atr=5.0,
        )

    def test_paper_fill_uses_next_open_and_target(self) -> None:
        settings = settings_from_preset(
            "test", "SPY", quantity=10, tp_channel_pct=0.5, stop_ticks=100
        )
        session = PaperSession.from_config(settings.__dict__)
        session.seed(_warmup_frame(20))
        session.pending_signal = self._signal()
        trade = session.on_bar(
            pd.Timestamp("2026-01-05 09:31", tz=NY_TZ),
            open_price=100.0,
            high=102.0,
            low=99.9,
            close=101.0,
        )
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.exit_reason, "TARGET")
        self.assertEqual(trade.entry_price, 100.01)
        self.assertGreater(trade.net_pnl, 0)
        self.assertEqual(session.position, None)

    def test_both_levels_hit_uses_stop_first(self) -> None:
        settings = settings_from_preset("test", "SPY", quantity=10)
        session = PaperSession.from_config(settings.__dict__)
        session.seed(_warmup_frame(20))
        session.pending_signal = self._signal()
        trade = session.on_bar(
            pd.Timestamp("2026-01-05 09:31", tz=NY_TZ),
            open_price=100.0,
            high=103.0,
            low=97.0,
            close=100.0,
        )
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.exit_reason, "STOP")
        self.assertLess(trade.net_pnl, 0)

    def test_flat_time_blocks_new_entries(self) -> None:
        settings = settings_from_preset("test", "SPY", quantity=10)
        session = PaperSession.from_config(settings.__dict__)
        session.seed(_warmup_frame(20))
        session.pending_signal = self._signal()
        trade = session.on_bar(
            pd.Timestamp("2026-01-05 15:55", tz=NY_TZ),
            open_price=100.0,
            high=100.1,
            low=99.9,
            close=100.0,
        )
        self.assertIsNone(trade)
        self.assertIsNone(session.position)

    def test_config_validation(self) -> None:
        with self.assertRaises(ValueError):
            RunnerSettings.from_dict({"runner_id": "x", "symbol": "SPY", "bogus": 1})
        with self.assertRaises(ValueError):
            PaperSession.from_config(
                {"runner_id": "x", "symbol": "YM", "quantity": 1}
            )
