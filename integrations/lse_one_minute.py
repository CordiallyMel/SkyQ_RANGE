"""One-minute LSE source and shared reader.

The public LSE feed delivers ticks.  ``lse_feed`` already turns those ticks
into durable bars; this module changes the bar interval to one minute and
caches the archive warmup separately from the five-minute reference path.

Only the bridge process opens the WebSocket.  Any number of paper runners read
the bridge's warmup parquet, append-only live CSV and status heartbeat.  That
matters on LSE's free plan, which allows one connection per key.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

from config.settings import MIN_HISTORY_BARS, NY_TZ
from integrations.lse_feed import (
    DEFAULT_BAR_GRACE_SECONDS,
    DEFAULT_WARMUP_DAYS,
    SHARED_STATUS_SCHEMA,
    FiveMinuteBarBuilder,
    LiveBarCache,
    LseArchive,
    LseFeedError,
    LseSignalSource,
    LseTickFeed,
    RTH_OPEN,
    SharedLseSignalSource,
    _atomic_text,
    _atomic_write,
    _local,
    _normalise_ohlcv,
)

log = logging.getLogger("skyq.range.lse_1m")

BAR_COLUMNS = ("open", "high", "low", "close", "volume")
ONE_MINUTE = 1
DEFAULT_STATUS_STALE_SECONDS = 15.0


class OneMinuteBarCache(LiveBarCache):
    """Append-only one-minute cache, kept separate from the 5m reference feed."""

    def path(self, day: date) -> Path:
        return self.directory / f"{self.symbol}_{day.isoformat()}_1m.csv"


def one_minute_status_path(cache_dir: str | Path | None, symbol: str) -> Path:
    root = (
        Path(cache_dir)
        if cache_dir
        else Path(__file__).resolve().parents[1] / "data" / "lse"
    )
    return root / "shared" / f"{symbol.upper()}_1m_status.json"


def _warmup_paths(cache_root: Path, symbol: str) -> tuple[Path, Path]:
    base = f"{symbol.upper()}_1m_warmup"
    return cache_root / f"{base}.parquet", cache_root / f"{base}.json"


class LseOneMinuteSource(LseSignalSource):
    """Archive warmup plus live ticks, emitted as completed one-minute bars.

    The parent class supplies the audited tick builder, reconnect/degraded
    semantics, poll loop and status text.  Only the warmup, cache and bar
    interval differ here.
    """

    def __init__(
        self,
        symbol: str,
        *,
        key_file: str | Path | None = None,
        warmup_days: int = DEFAULT_WARMUP_DAYS,
        cache_dir: str | Path | None = None,
        bar_grace_seconds: float = DEFAULT_BAR_GRACE_SECONDS,
        archive: LseArchive | None = None,
        api_key: str | None = None,
        feed_factory: Callable[..., LseTickFeed] = LseTickFeed,
    ) -> None:
        super().__init__(
            symbol,
            key_file=key_file,
            warmup_days=warmup_days,
            cache_dir=cache_dir,
            bar_grace_seconds=bar_grace_seconds,
            archive=archive,
            api_key=api_key,
            feed_factory=feed_factory,
        )
        self.warmup_path, self.warmup_meta_path = _warmup_paths(
            self.cache_root, self.symbol
        )

    def _read_cached_warmup(self, through: date) -> pd.DataFrame | None:
        if not self.warmup_path.is_file() or not self.warmup_meta_path.is_file():
            return None
        try:
            meta = json.loads(self.warmup_meta_path.read_text(encoding="utf-8"))
            if meta.get("through") != through.isoformat():
                return None
            frame = pd.read_parquet(self.warmup_path)
            frame = _normalise_ohlcv(frame)
        except Exception as exc:  # corrupt cache should trigger a refetch
            log.warning("ignoring one-minute warmup cache: %s", exc)
            return None
        if len(frame) < MIN_HISTORY_BARS:
            return None
        return frame

    def _warmup_1m(self, current: datetime) -> pd.DataFrame:
        through = current.date() - timedelta(days=1)
        cached = self._read_cached_warmup(through)
        if cached is not None:
            log.info(
                "one-minute warmup cache hit: %d bars through %s",
                len(cached),
                through,
            )
            return cached

        start = through - timedelta(days=self.warmup_days)
        log.info(
            "downloading one-minute warmup for %s: %s through %s",
            self.symbol,
            start,
            through,
        )
        raw = self.archive.bars(
            self.symbol,
            start=start,
            end=through,
            timeframe="1m",
        )
        frame = _normalise_ohlcv(raw)
        if len(frame) < MIN_HISTORY_BARS:
            raise LseFeedError(
                f"LSE returned only {len(frame)} one-minute warmup bars for "
                f"{self.symbol}; need at least {MIN_HISTORY_BARS}. "
                "Check the archive entitlement and requested date range."
            )
        self.cache_root.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.warmup_path, frame.to_parquet())
        _atomic_text(
            self.warmup_meta_path,
            json.dumps(
                {
                    "symbol": self.symbol,
                    "timeframe": "1m",
                    "through": through.isoformat(),
                    "rows": int(len(frame)),
                    "first": frame.index.min().isoformat(),
                    "last": frame.index.max().isoformat(),
                    "generated_at": datetime.now(tz=NY_TZ).isoformat(),
                },
                indent=2,
            )
            + "\n",
        )
        return frame

    def start(self, *, now: datetime | None = None, timeout: float = 45.0) -> None:
        if self._started:
            return
        current = _local(now or datetime.now(tz=NY_TZ))
        self._started_at = current
        self._started_before_open = current.time() < RTH_OPEN
        if not self._started_before_open and current.weekday() < 5:
            self._degraded_reason = (
                "started at or after 09:30 ET; the LSE archive has no current-day "
                "bars and the live protocol cannot replay the missed morning"
            )
            log.critical("LSE SESSION DEGRADED: %s", self._degraded_reason)

        self._warmup = self._warmup_1m(current)
        self._cache = OneMinuteBarCache(self.cache_root / "live", self.symbol)
        self._loaded_today = self._cache.load(current.date())
        self._builder = FiveMinuteBarBuilder(
            self.symbol,
            minutes=ONE_MINUTE,
            session_date=current.date(),
            on_bar=self._on_bar,
        )
        self._feed = self._feed_factory(
            self.api_key,
            self.symbol,
            self._builder.add_tick,
            on_disconnect=self._on_disconnect,
        )
        self._feed.start()
        try:
            self._feed.wait_ready(timeout=timeout)
        except Exception:
            self._feed.stop()
            raise
        self._feed_ready_at = datetime.now(tz=NY_TZ)
        if (
            current.time() < RTH_OPEN
            and self._feed_ready_at.time() >= RTH_OPEN
            and self._feed_ready_at.weekday() < 5
        ):
            self._degraded_reason = (
                "LSE feed became ready after 09:30 ET; the opening ticks were "
                "missed while warmup or authentication was in progress"
            )
            log.critical("LSE SESSION DEGRADED: %s", self._degraded_reason)
        self._started_before_open = bool(
            current.time() < RTH_OPEN
            and self._feed_ready_at.time() < RTH_OPEN
        )
        self._started = True
        log.info(
            "LSE 1m source ready %s | warmup=%d bars through %s | today_cache=%d "
            "bars | %s",
            self.symbol,
            len(self._warmup),
            self._warmup.index.max().date() if not self._warmup.empty else "?",
            len(self._loaded_today),
            self._feed.status(),
        )

    def status_payload(self, *, fatal: str | None = None) -> dict[str, object]:
        """The JSON heartbeat consumed by one or more shared paper runners."""
        tick = self.latest_tick
        last_tick = (
            {
                "symbol": tick.symbol,
                "price": tick.price,
                "volume": tick.volume,
                "timestamp": tick.timestamp.isoformat(),
                "bid": tick.bid,
                "ask": tick.ask,
            }
            if tick is not None
            else None
        )
        return {
            "schema": SHARED_STATUS_SCHEMA,
            "symbol": self.symbol,
            "timeframe": "1m",
            "session_date": self._started_at.date().isoformat()
            if self._started_at
            else None,
            "ready": bool(self._started),
            "started_before_open": bool(self._started_before_open),
            "degraded_reason": self._degraded_reason,
            "fatal": fatal,
            "error": fatal,
            "warmup_cache": str(self.warmup_path),
            "warmup_rows": int(len(self._warmup)),
            "today_bars": int(self.today_bars),
            "last_tick": last_tick,
            "feed": self._feed.status() if self._feed is not None else "not started",
            "feed_status": self._feed.status()
            if self._feed is not None
            else "not started",
            "updated_at": datetime.now(tz=NY_TZ).isoformat(),
            "pid": __import__("os").getpid(),
        }


class SharedLseOneMinuteSource(SharedLseSignalSource):
    """Read-only access to a bridge's one-minute cache and heartbeat."""

    def __init__(
        self,
        symbol: str,
        *,
        cache_dir: str | Path | None = None,
        status_timeout: float = DEFAULT_STATUS_STALE_SECONDS,
    ) -> None:
        super().__init__(
            symbol,
            cache_dir=cache_dir,
            status_timeout=status_timeout,
        )
        self.status_path = one_minute_status_path(self.cache_root, self.symbol)
        self.warmup_path = _warmup_paths(self.cache_root, self.symbol)[0]
        self._live = OneMinuteBarCache(self.cache_root / "live", self.symbol)

    def status_payload(self) -> dict[str, object]:
        return dict(self._status)

    def wait_for_bridge(
        self, *, now: datetime | None = None, timeout: float = 45.0
    ) -> None:
        """Wait for a live bridge and load its bars (no network connection)."""
        self.start(now=now, timeout=timeout)


def write_status(path: Path, payload: dict[str, object]) -> None:
    _atomic_text(path, json.dumps(payload, indent=2, default=str) + "\n")


def status_is_stale(path: Path, *, max_age_seconds: float = 15.0) -> bool:
    if not path.is_file():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = pd.Timestamp(payload["updated_at"])
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize(NY_TZ)
        age = (datetime.now(tz=NY_TZ) - stamp.tz_convert(NY_TZ)).total_seconds()
        return age > max_age_seconds
    except Exception:
        return True


__all__ = [
    "BAR_COLUMNS",
    "DEFAULT_STATUS_STALE_SECONDS",
    "LseOneMinuteSource",
    "OneMinuteBarCache",
    "SharedLseOneMinuteSource",
    "one_minute_status_path",
    "status_is_stale",
    "write_status",
]
