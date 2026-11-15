"""LSE Data feed and 5-minute bar source for the SkyQ Sigma runner.

The LSE WebSocket is a live tick feed.  Its current protocol no longer supports
the old 24-hour replay: subscribing returns the symbol's last price and then
live updates only.  That means two different data paths are required:

* **Warmup:** completed 5-minute RTH bars from the LSE archive, through the
  previous session.  This is the same provider and the same trade-built bars
  used by the London replay.
* **Today:** trade ticks from the LSE WebSocket, aggregated into 5-minute bars
  using left-labelled, left-closed buckets in New York time.

Only ticks with ``volume > 0`` are trades.  The WebSocket sends ``volume = 0``
for quote/last-price updates, and including those would corrupt OHLC and VWAP.
The aggregation rule was checked against the LSE archive for 2026-11-12:
278,791 SPY trades produced 78 RTH bars with exact OHLCV parity.

The LSE free WebSocket plan allows one connection per key on data-ws (the
``authenticated`` reply reports ``max_connections``); paid plans report a
higher cap.  If the socket drops during the session, the missing trades cannot
be reconstructed from the archive until the day is over.  The source therefore
marks the session degraded and the runner must stop rather than trade on a
partial VWAP.  Start the bridge before 09:30 ET.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib import error, parse, request

import pandas as pd

from config.settings import BAR_MINUTES, MIN_HISTORY_BARS, NY_TZ

log = logging.getLogger("skyq.sigma.lse")

LSE_WS_URL = "wss://data-ws.londonstrategicedge.com"
LSE_BASE_URL = "https://download.londonstrategicedge.com/v3"
LSE_MARKET = "us_stocks"
DEFAULT_WARMUP_DAYS = 45
DEFAULT_BAR_GRACE_SECONDS = 5.0
RTH_OPEN = clock_time(9, 30)
RTH_CLOSE = clock_time(16, 0)
# A disconnect before the open can still lose the opening trades if the socket
# only returns after 09:30.  Treat 09:25-16:00 as the protected window.
FEED_PROTECT_FROM = clock_time(9, 25)
FEED_PROTECT_TO = clock_time(16, 0)
_BAR_COLUMNS = ("open", "high", "low", "close", "volume")


class LseFeedError(RuntimeError):
    """Transport or protocol failure in the LSE feed."""


class LseDataUnavailable(LseFeedError):
    """The archive has no stored rows for the selection."""


class LseAuthError(LseFeedError):
    """The key was refused, revoked or exhausted."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_api_key(key_file: str | Path | None = None) -> str:
    """Load the LSE key from the environment, an explicit file, or .env.

    The value is never logged or included in an exception.  An explicit file
    may contain either the bare key or a ``LSE_API_KEY=...`` line.
    """
    environment = os.getenv("LSE_API_KEY", "").strip()
    if environment:
        return environment

    candidates: list[Path] = []
    if key_file:
        candidates.append(Path(key_file).expanduser())
    candidates.append(_project_root() / ".env")

    for candidate in candidates:
        if not candidate.exists() or not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8")
        bare = text.strip()
        if bare and "\n" not in bare and "=" not in bare and not bare.startswith("#"):
            return bare.strip('"').strip("'")
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() == "LSE_API_KEY":
                value = value.strip().strip('"').strip("'")
                if value:
                    return value
    raise LseFeedError(
        "No LSE_API_KEY. Set it in the environment or in this repository's .env."
    )


def parse_timestamp(value: object) -> datetime:
    """Parse every UTC timestamp shape the LSE feed documents."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("tick has no timestamp")
        # Python accepts at most microseconds; LSE sometimes sends nanoseconds.
        text = re.sub(r"(\.\d{6})\d+", r"\1", text)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=NY_TZ)
    return value.astimezone(NY_TZ)


def _is_rth(value: datetime) -> bool:
    local = _local(value)
    return local.weekday() < 5 and RTH_OPEN <= local.time() < RTH_CLOSE


def _feed_protected(value: datetime) -> bool:
    local = _local(value)
    return local.weekday() < 5 and FEED_PROTECT_FROM <= local.time() < FEED_PROTECT_TO


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _atomic_text(path: Path, text: str) -> None:
    _atomic_write(path, text.encode("utf-8"))


def _normalise_ohlcv(frame: pd.DataFrame, *, rth_only: bool = True) -> pd.DataFrame:
    """Normalise an archive frame to a New-York-indexed RTH OHLCV frame."""
    if frame.empty:
        return pd.DataFrame(columns=list(_BAR_COLUMNS), dtype=float)
    data = frame.copy()
    data.columns = [str(column).strip().lower() for column in data.columns]
    if not isinstance(data.index, pd.DatetimeIndex):
        for column in ("time", "timestamp", "date", "datetime"):
            if column in data.columns:
                data = data.set_index(column)
                break
    if not isinstance(data.index, pd.DatetimeIndex):
        raise LseFeedError("archive bars have no time column")
    missing = [column for column in _BAR_COLUMNS if column not in data.columns]
    if missing:
        raise LseFeedError(f"archive bars are missing: {', '.join(missing)}")
    index = pd.DatetimeIndex(data.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    data.index = index.tz_convert(NY_TZ)
    data = data[~data.index.duplicated(keep="last")].sort_index()
    data = data.loc[:, list(_BAR_COLUMNS)].astype(float)
    if rth_only:
        minutes = data.index.hour * 60 + data.index.minute
        data = data[(minutes >= 9 * 60 + 30) & (minutes < 16 * 60)]
    return data


@dataclass(frozen=True)
class LseTick:
    """One LSE WebSocket tick."""

    symbol: str
    price: float
    volume: float
    timestamp: datetime
    bid: float | None = None
    ask: float | None = None

    @classmethod
    def from_message(cls, message: dict) -> "LseTick":
        raw_timestamp = message.get("ts")
        if raw_timestamp is None:
            raise ValueError("tick has no ts")
        price = float(message.get("price") or 0.0)
        if price <= 0:
            raise ValueError(f"tick has invalid price {price!r}")
        bid = message.get("bid")
        ask = message.get("ask")
        return cls(
            symbol=str(message.get("symbol") or ""),
            price=price,
            volume=float(message.get("volume") or 0.0),
            timestamp=parse_timestamp(raw_timestamp),
            bid=float(bid) if bid is not None else None,
            ask=float(ask) if ask is not None else None,
        )

    @property
    def is_trade(self) -> bool:
        """The feed uses ``volume == 0`` for a quote/last-price update."""
        return self.volume > 0 and self.price > 0


@dataclass(frozen=True)
class LseBar:
    """One completed five-minute bar built from LSE trades."""

    time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int

    @property
    def close_time(self) -> pd.Timestamp:
        return self.time + pd.Timedelta(minutes=BAR_MINUTES)

    def as_row(self) -> dict[str, float]:
        return {
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class FiveMinuteBarBuilder:
    """Thread-safe LSE trade-tick to 5-minute OHLCV builder.

    Buckets are left-labelled and left-closed, matching
    :func:`src.indicators.resample_bars` and the LSE archive.  Ticks are used
    by their source timestamp, not their arrival time, so a slightly late tick
    still lands in the correct bar.  A bar is not exposed until ``grace``
    seconds after its close, giving late prints a chance to arrive.
    """

    def __init__(
        self,
        symbol: str = "SPY",
        *,
        minutes: int = BAR_MINUTES,
        session_date: date | None = None,
        on_bar: Callable[[LseBar], None] | None = None,
    ) -> None:
        if minutes <= 0:
            raise ValueError("minutes must be positive")
        self.symbol = symbol.upper()
        self.minutes = int(minutes)
        self._active_date = session_date
        self.on_bar = on_bar
        self._bars: dict[pd.Timestamp, dict[str, object]] = {}
        self._completed: dict[pd.Timestamp, LseBar] = {}
        self._closed: set[pd.Timestamp] = set()
        self._gap_reason: str | None = None
        self._lock = threading.RLock()

    def set_active_date(self, day: date) -> None:
        """Accept only trades from this New-York session date from now on."""
        with self._lock:
            self._active_date = day

    @property
    def active_date(self) -> date | None:
        with self._lock:
            return self._active_date

    @property
    def gap_reason(self) -> str | None:
        with self._lock:
            return self._gap_reason

    def _note_gap(self, previous: pd.Timestamp, bucket: pd.Timestamp) -> None:
        if self._gap_reason is not None:
            return
        if bucket.time() > RTH_OPEN and previous.date() != bucket.date():
            self._gap_reason = (
                f"first RTH bar arrived at {bucket.isoformat()} instead of 09:30"
            )
        elif previous.date() == bucket.date():
            expected = previous + timedelta(minutes=self.minutes)
            if bucket > expected:
                self._gap_reason = (
                    f"missing LSE bars between {previous.isoformat()} and "
                    f"{bucket.isoformat()}"
                )

    def add_tick(self, tick: LseTick) -> bool:
        """Add one tick; returns True only when a trade was included."""
        if not tick.is_trade:
            return False
        local = _local(tick.timestamp)
        with self._lock:
            if self._active_date is not None and local.date() != self._active_date:
                # The initial subscription tick is the last stored trade and
                # may be from the previous session.  It must never replace the
                # complete warmup bar with a one-tick partial bar.
                return False
        minute = local.hour * 60 + local.minute
        if not (9 * 60 + 30 <= minute < 16 * 60):
            return False
        bucket = pd.Timestamp(local).floor(f"{self.minutes}min")
        with self._lock:
            if bucket in self._closed:
                log.debug("late tick for closed LSE bar %s ignored", bucket)
                return False
            bar = self._bars.get(bucket)
            if bar is None:
                self._bars[bucket] = {
                    "open": tick.price,
                    "high": tick.price,
                    "low": tick.price,
                    "close": tick.price,
                    "volume": float(tick.volume),
                    "trades": 1,
                    "open_ts": tick.timestamp,
                    "close_ts": tick.timestamp,
                }
                return True
            if tick.timestamp < bar["open_ts"]:
                bar["open"] = tick.price
                bar["open_ts"] = tick.timestamp
            if tick.timestamp >= bar["close_ts"]:
                bar["close"] = tick.price
                bar["close_ts"] = tick.timestamp
            bar["high"] = max(float(bar["high"]), tick.price)
            bar["low"] = min(float(bar["low"]), tick.price)
            bar["volume"] = float(bar["volume"]) + float(tick.volume)
            bar["trades"] = int(bar["trades"]) + 1
            return True

    def finalize(self, now: datetime, grace_seconds: float = 0.0) -> list[LseBar]:
        """Close every bar whose bucket end plus grace is at or before ``now``."""
        local_now = _local(now)
        cutoff = pd.Timestamp(local_now) - pd.Timedelta(seconds=max(0.0, grace_seconds))
        width = pd.Timedelta(minutes=self.minutes)
        ready: list[tuple[pd.Timestamp, dict[str, object]]] = []
        with self._lock:
            for bucket, bar in list(self._bars.items()):
                if bucket + width <= cutoff:
                    ready.append((bucket, bar))
                    del self._bars[bucket]
                    self._closed.add(bucket)
        completed: list[LseBar] = []
        for bucket, bar in sorted(ready, key=lambda item: item[0]):
            completed.append(
                LseBar(
                    time=bucket,
                    open=float(bar["open"]),
                    high=float(bar["high"]),
                    low=float(bar["low"]),
                    close=float(bar["close"]),
                    volume=float(bar["volume"]),
                    trades=int(bar["trades"]),
                )
            )
        for bar in completed:
            with self._lock:
                previous = max(self._completed) if self._completed else None
                if previous is not None:
                    self._note_gap(previous, bar.time)
                elif bar.time.time() > RTH_OPEN:
                    self._note_gap(bar.time, bar.time)
                self._completed[bar.time] = bar
            if self.on_bar is not None:
                try:
                    self.on_bar(bar)
                except Exception:
                    log.exception("LSE on_bar callback failed")
        return completed

    def frame(self) -> pd.DataFrame:
        """All finalized bars as an OHLCV frame."""
        with self._lock:
            rows = sorted(self._completed.items(), key=lambda item: item[0])
        if not rows:
            return pd.DataFrame(
                columns=list(_BAR_COLUMNS),
                dtype=float,
                index=pd.DatetimeIndex([], name="time"),
            )
        data = pd.DataFrame(
            [bar.as_row() for _, bar in rows],
            index=pd.DatetimeIndex([bucket for bucket, _ in rows], name="time"),
        )
        return data.sort_index().astype(float)

    def current_partial(self) -> pd.DataFrame:
        """The current, not-yet-finalized bar for diagnostics."""
        with self._lock:
            rows = [
                (bucket, bar)
                for bucket, bar in self._bars.items()
                if bucket not in self._closed
            ]
        if not rows:
            return pd.DataFrame(columns=list(_BAR_COLUMNS), dtype=float)
        return pd.DataFrame(
            [
                {
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": float(bar["volume"]),
                }
                for _, bar in rows
            ],
            index=pd.DatetimeIndex([bucket for bucket, _ in rows], name="time"),
        ).sort_index()


class LiveBarCache:
    """Durable, append-only day cache of completed LSE bars."""

    def __init__(self, directory: str | Path, symbol: str) -> None:
        self.directory = Path(directory)
        self.symbol = symbol.upper()

    def path(self, day: date) -> Path:
        return self.directory / f"{self.symbol}_{day.isoformat()}_5m.csv"

    def load(self, day: date) -> pd.DataFrame:
        path = self.path(day)
        if not path.exists():
            return pd.DataFrame(
                columns=list(_BAR_COLUMNS),
                dtype=float,
                index=pd.DatetimeIndex([], name="time"),
            )
        try:
            frame = pd.read_csv(path)
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            log.warning("could not read LSE live cache %s: %s", path, exc)
            return pd.DataFrame(
                columns=list(_BAR_COLUMNS),
                dtype=float,
                index=pd.DatetimeIndex([], name="time"),
            )
        try:
            frame["time"] = pd.to_datetime(frame["time"], format="mixed", utc=True)
            frame = frame.set_index("time").tz_convert(NY_TZ)
            frame = frame.loc[:, list(_BAR_COLUMNS)].astype(float)
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("malformed LSE live cache %s: %s", path, exc)
            return pd.DataFrame(
                columns=list(_BAR_COLUMNS),
                dtype=float,
                index=pd.DatetimeIndex([], name="time"),
            )
        return frame[~frame.index.duplicated(keep="last")].sort_index()

    def append(self, bar: LseBar) -> None:
        path = self.path(bar.time.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        header = not path.exists() or path.stat().st_size == 0
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle, fieldnames=("time", *_BAR_COLUMNS, "trades")
            )
            if header:
                writer.writeheader()
            writer.writerow(
                {
                    "time": bar.time.isoformat(),
                    "open": f"{bar.open:.10g}",
                    "high": f"{bar.high:.10g}",
                    "low": f"{bar.low:.10g}",
                    "close": f"{bar.close:.10g}",
                    "volume": f"{bar.volume:.10g}",
                    "trades": bar.trades,
                }
            )
            handle.flush()
            os.fsync(handle.fileno())


class LseArchive:
    """Minimal LSE Data 3.0 archive client for RTH warmup and verification."""

    RETRY_STATUS = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = LSE_BASE_URL,
        timeout: float = 120.0,
        poll_seconds: float = 1.0,
        max_retries: int = 3,
    ) -> None:
        if not api_key:
            raise ValueError("an LSE API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.poll_seconds = max(0.25, float(poll_seconds))
        self.max_retries = max(0, int(max_retries))

    def _open(self, path: str, params: dict | None = None):
        query = parse.urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        )
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += "?" + query
        req = request.Request(
            url,
            headers={
                "X-API-Key": self.api_key,
                "Accept": "application/octet-stream, application/json",
            },
        )
        attempt = 0
        while True:
            try:
                return request.urlopen(req, timeout=self.timeout)
            except error.HTTPError as exc:
                body = exc.read()
                payload: dict = {}
                try:
                    payload = json.loads(body)
                except (ValueError, TypeError):
                    pass
                code = str(payload.get("code") or exc.code)
                message = str(payload.get("message") or exc.reason or "")
                if exc.code in self.RETRY_STATUS and attempt < self.max_retries:
                    retry_after = exc.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 2.0**attempt
                    except (TypeError, ValueError):
                        delay = float(2**attempt)
                    time_module.sleep(min(60.0, max(1.0, delay)))
                    attempt += 1
                    continue
                if code in {"data-unavailable", "pair-unknown", "date-not-trading"}:
                    raise LseDataUnavailable(
                        f"{code}: {message}"
                    ) from exc
                if exc.code in {401, 403} or code in {
                    "INVALID_KEY",
                    "MISSING_KEY",
                    "QUOTA_EXCEEDED",
                    "KEY_REVOKED",
                }:
                    raise LseAuthError(f"{code}: {message}") from exc
                raise LseFeedError(f"{code}: {message}") from exc
            except error.URLError as exc:
                if attempt >= self.max_retries:
                    raise LseFeedError(f"LSE transport failed: {exc.reason}") from exc
                time_module.sleep(min(30.0, 2.0**attempt))
                attempt += 1

    def _export_bytes(self, params: dict) -> bytes:
        response = self._open("export", params)
        with response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
        if content_type.startswith("application/json"):
            job = json.loads(raw)
            job_id = str(job.get("job_id") or "")
            if not job_id:
                raise LseFeedError(f"LSE export did not return a job id: {job}")
            deadline = time_module.monotonic() + 300.0
            while time_module.monotonic() < deadline:
                with self._open(f"jobs/{job_id}") as status_response:
                    status = json.loads(status_response.read())
                state = str(status.get("status") or "")
                if state == "ready":
                    with self._open(f"jobs/{job_id}/file") as file_response:
                        return file_response.read()
                if state in {"failed", "expired"}:
                    raise LseFeedError(
                        f"LSE export job {job_id} {state}: {status.get('error') or ''}"
                    )
                time_module.sleep(self.poll_seconds)
            raise LseFeedError(f"LSE export job {job_id} timed out")
        return raw

    def export(
        self,
        *,
        symbol: str,
        timeframe: str,
        start: date,
        end: date,
        data: str = "candles",
        fmt: str = "parquet",
    ) -> bytes:
        return self._export_bytes(
            {
                "market": LSE_MARKET,
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "data": data,
                "from": f"{start:%Y-%m-%d}",
                "to": f"{end:%Y-%m-%d}",
                "format": fmt,
                "adjusted": 1,
            }
        )

    def bars(
        self, symbol: str, start: date, end: date, timeframe: str = "5m"
    ) -> pd.DataFrame:
        raw = self.export(symbol=symbol, timeframe=timeframe, start=start, end=end)
        try:
            frame = pd.read_parquet(io.BytesIO(raw))
        except Exception as exc:
            raise LseFeedError(f"LSE archive returned unreadable {timeframe} bars") from exc
        return _normalise_ohlcv(frame)

    def ticks(self, symbol: str, day: date) -> pd.DataFrame:
        """The stored trade slice, used by the parity verifier."""
        raw = self.export(
            symbol=symbol, timeframe="tick", start=day, end=day, data="candles"
        )
        try:
            frame = pd.read_parquet(io.BytesIO(raw))
        except Exception as exc:
            raise LseFeedError("LSE archive returned an unreadable trade slice") from exc
        frame = frame.copy()
        if "time" not in frame.columns:
            raise LseFeedError("LSE trade slice has no time column")
        frame["time"] = pd.to_datetime(frame["time"], utc=True)
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
        frame["size"] = pd.to_numeric(frame.get("size"), errors="coerce").fillna(0.0)
        if "kind" in frame.columns:
            frame = frame[frame["kind"].astype(str).str.lower().eq("trade")]
        return frame.dropna(subset=["price"]).sort_values("time").reset_index(drop=True)

    def warmup(
        self,
        symbol: str,
        *,
        days: int = DEFAULT_WARMUP_DAYS,
        cache_dir: str | Path | None = None,
        now: datetime | None = None,
        required_bars: int = MIN_HISTORY_BARS,
    ) -> pd.DataFrame:
        """Load completed RTH 5-minute bars through the previous session.

        The LSE archive does not make the current session available until it is
        over, so the warmup ends yesterday.  Today's bars come only from the
        live WebSocket.
        """
        current = _local(now or datetime.now(tz=NY_TZ))
        through = current.date() - timedelta(days=1)
        start = through - timedelta(days=max(1, int(days)))
        cache_root = (
            Path(cache_dir) if cache_dir else Path(__file__).resolve().parents[1] / "data" / "lse"
        )
        cache_root = Path(cache_root)
        cache_path = cache_root / f"{symbol.upper()}_5m_warmup.parquet"
        meta_path = cache_path.with_suffix(".meta.json")

        if cache_path.exists() and meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("through") == through.isoformat():
                    cached = pd.read_parquet(cache_path)
                    cached = _normalise_ohlcv(cached)
                    if len(cached) >= required_bars:
                        log.info(
                            "LSE warmup cache: %d bars through %s",
                            len(cached),
                            through,
                        )
                        return cached
            except (OSError, ValueError, KeyError) as exc:
                log.warning("LSE warmup cache unreadable: %s", exc)

        fresh = self.bars(symbol, start, through)
        if len(fresh) < required_bars:
            raise LseFeedError(
                f"LSE warmup returned only {len(fresh)} RTH bars; "
                f"{required_bars} are required"
            )
        _atomic_write(
            cache_path,
            fresh.reset_index().to_parquet(index=False),
        )
        _atomic_text(
            meta_path,
            json.dumps(
                {
                    "symbol": symbol.upper(),
                    "through": through.isoformat(),
                    "bars": len(fresh),
                    "fetched_at": datetime.now(tz=timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        log.info(
            "LSE warmup downloaded: %d RTH bars through %s",
            len(fresh),
            through,
        )
        return fresh


class LseTickFeed:
    """Live LSE WebSocket client running in a background thread."""

    FATAL_CODES = frozenset(
        {
            "INVALID_KEY",
            "MISSING_KEY",
            "QUOTA_EXCEEDED",
            "KEY_REVOKED",
            "TOO_MANY_CONNECTIONS",
            "TOO_MANY_CONNECTIONS_FROM_ADDRESS",
            "CONNECTION_REPLACED",
            "KEY_IN_USE",
        }
    )

    def __init__(
        self,
        api_key: str,
        symbol: str,
        on_tick: Callable[[LseTick], None],
        *,
        url: str = LSE_WS_URL,
        on_disconnect: Callable[[str], None] | None = None,
        auth_timeout: float = 25.0,
    ) -> None:
        self.api_key = api_key
        self.symbol = symbol.upper()
        self.on_tick = on_tick
        self.url = url
        self.on_disconnect = on_disconnect
        self.auth_timeout = float(auth_timeout)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket = None
        self._lock = threading.Lock()
        self._error: str | None = None
        self._fatal = False
        self._authenticated = False
        self._connected_at: datetime | None = None
        self._last_disconnect_at: datetime | None = None
        self._last_tick: LseTick | None = None
        self._last_message_at: datetime | None = None
        self._disconnects = 0
        self._tier = ""
        self._max_subscriptions: int | None = None
        self._max_connections: int | None = None

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def fatal(self) -> bool:
        return self._fatal

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def last_tick(self) -> LseTick | None:
        return self._last_tick

    @property
    def last_message_at(self) -> datetime | None:
        return self._last_message_at

    @property
    def connected_at(self) -> datetime | None:
        return self._connected_at

    @property
    def tier(self) -> str:
        return self._tier

    @property
    def max_connections(self) -> int | None:
        return self._max_connections

    @property
    def disconnects(self) -> int:
        return self._disconnects

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"lse-feed-{self.symbol}",
            daemon=True,
        )
        self._thread.start()

    def wait_ready(self, timeout: float = 30.0) -> None:
        deadline = time_module.monotonic() + max(0.1, timeout)
        while not self._ready.is_set():
            if self._fatal:
                raise LseFeedError(
                    self._error or "LSE WebSocket refused the key"
                )
            remaining = deadline - time_module.monotonic()
            if remaining <= 0:
                raise LseFeedError(
                    self._error or "LSE WebSocket did not subscribe in time"
                )
            self._ready.wait(min(0.25, remaining))
        if self._fatal:
            raise LseFeedError(self._error or "LSE WebSocket refused the key")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._ready.clear()
        with self._lock:
            socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))

    def _mark_disconnect(self, reason: str) -> None:
        self._ready.clear()
        self._authenticated = False
        self._connected_at = None
        self._last_disconnect_at = datetime.now(tz=NY_TZ)
        self._disconnects += 1
        if self.on_disconnect is not None and not self._stop.is_set():
            try:
                self.on_disconnect(reason)
            except Exception:
                log.exception("LSE disconnect callback failed")

    def _run(self) -> None:
        try:
            from websockets.exceptions import ConnectionClosed
            from websockets.sync.client import connect
        except Exception as exc:
            self._fatal = True
            self._error = f"websockets package unavailable: {exc}"
            return

        backoff = 1.0
        while not self._stop.is_set():
            try:
                with connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=30,
                    open_timeout=15,
                    close_timeout=5,
                    max_size=None,
                ) as socket:
                    with self._lock:
                        self._socket = socket
                    self._error = None
                    self._authenticated = False
                    self._ready.clear()
                    welcome = json.loads(socket.recv(timeout=15))
                    if welcome.get("type") != "welcome":
                        raise LseFeedError(
                            f"unexpected LSE greeting: {welcome.get('type')}"
                        )
                    socket.send(
                        json.dumps({"action": "auth", "api_key": self.api_key})
                    )
                    subscribed = False
                    last_ping = time_module.monotonic()
                    while not self._stop.is_set():
                        try:
                            raw = socket.recv(timeout=1.0)
                        except TimeoutError:
                            if time_module.monotonic() - last_ping >= 25.0:
                                socket.send(json.dumps({"action": "ping"}))
                                last_ping = time_module.monotonic()
                            continue
                        message = json.loads(raw)
                        message_type = message.get("type")
                        self._last_message_at = datetime.now(tz=NY_TZ)
                        if message_type == "authenticated":
                            self._authenticated = True
                            self._tier = str(message.get("tier") or "")
                            maximum = message.get("max_subscriptions")
                            self._max_subscriptions = (
                                int(maximum) if maximum is not None else None
                            )
                            maximum_connections = message.get("max_connections")
                            self._max_connections = (
                                int(maximum_connections)
                                if maximum_connections is not None
                                else None
                            )
                            log.info(
                                "LSE authenticated tier=%s key_type=%s max_symbols=%s "
                                "max_connections=%s",
                                self._tier,
                                message.get("key_type"),
                                self._max_subscriptions,
                                self._max_connections,
                            )
                            socket.send(
                                json.dumps(
                                    {"action": "subscribe", "symbol": self.symbol}
                                )
                            )
                            last_ping = time_module.monotonic()
                        elif message_type == "subscribed":
                            subscribed = True
                            self._connected_at = datetime.now(tz=NY_TZ)
                            self._ready.set()
                            log.info(
                                "LSE subscribed %s count=%s max=%s",
                                message.get("symbol", self.symbol),
                                message.get("count"),
                                message.get("max"),
                            )
                        elif message_type == "tick":
                            try:
                                tick = LseTick.from_message(message)
                            except (TypeError, ValueError) as exc:
                                log.warning("ignored malformed LSE tick: %s", exc)
                                continue
                            self._last_tick = tick
                            try:
                                self.on_tick(tick)
                            except Exception:
                                log.exception("LSE tick handler failed")
                        elif message_type == "error":
                            code = str(message.get("code") or "")
                            detail = str(message.get("message") or code)
                            self._error = f"{code}: {detail}" if code else detail
                            log.error("LSE WebSocket error %s", self._error)
                            if code in self.FATAL_CODES or not self._authenticated:
                                self._fatal = True
                                self._stop.set()
                                break
                        elif message_type == "pong":
                            last_ping = time_module.monotonic()
                        # welcome, subscribed and lifecycle confirmations need
                        # no further action.
                    if not subscribed and not self._stop.is_set():
                        raise LseFeedError("LSE socket closed before subscription")
                if not self._stop.is_set():
                    self._mark_disconnect("LSE WebSocket closed")
            except ConnectionClosed as exc:
                if self._stop.is_set():
                    break
                close_code = getattr(exc, "code", "?")
                close_reason = str(getattr(exc, "reason", "") or "")
                self._mark_disconnect(
                    f"LSE WebSocket closed ({close_code})"
                    + (f": {close_reason}" if close_reason else "")
                )
                conflict = {
                    "TOO_MANY_CONNECTIONS",
                    "TOO_MANY_CONNECTIONS_FROM_ADDRESS",
                    "CONNECTION_REPLACED",
                    "KEY_IN_USE",
                }
                if any(code in close_reason for code in conflict):
                    self._fatal = True
                    self._stop.set()
                    self._error = (
                        "another LSE connection is using this key or address; "
                        "this runner stopped instead of competing for the socket"
                    )
                    log.critical(self._error)
                    break
            except Exception as exc:
                if self._stop.is_set():
                    break
                self._error = str(exc)
                log.warning("LSE WebSocket session failed: %s", exc)
                self._mark_disconnect(str(exc))
            finally:
                with self._lock:
                    self._socket = None
                self._ready.clear()
                self._authenticated = False

            if self._fatal or self._stop.is_set():
                break
            time_module.sleep(backoff)
            backoff = min(30.0, backoff * 2.0)

    def status(self) -> str:
        state = "ready" if self.ready else "disconnected"
        if self.fatal:
            state = "fatal"
        tick = self._last_tick
        tick_text = (
            f"{tick.timestamp.isoformat()} {tick.price:g} vol={tick.volume:g}"
            if tick is not None
            else "no tick yet"
        )
        return (
            f"LSE {self.symbol} {state} | tier={self._tier or '?'} | "
            f"max_connections={self._max_connections if self._max_connections is not None else '?'} | "
            f"disconnects={self._disconnects} | {tick_text}"
            + (f" | error={self._error}" if self._error else "")
        )


class LseSignalSource:
    """Archive warmup + live LSE ticks, exposed as completed OHLCV bars."""

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
        self.symbol = symbol.upper()
        self.key_file = key_file
        self.warmup_days = max(1, int(warmup_days))
        self.cache_root = (
            Path(cache_dir)
            if cache_dir
            else Path(__file__).resolve().parents[1] / "data" / "lse"
        )
        self.bar_grace_seconds = max(0.0, float(bar_grace_seconds))
        self._archive = archive
        self._api_key = api_key
        self._feed_factory = feed_factory
        self._feed: LseTickFeed | None = None
        self._builder: FiveMinuteBarBuilder | None = None
        self._cache: LiveBarCache | None = None
        self._warmup = pd.DataFrame(columns=list(_BAR_COLUMNS), dtype=float)
        self._loaded_today = pd.DataFrame(columns=list(_BAR_COLUMNS), dtype=float)
        self._started_at: datetime | None = None
        self._feed_ready_at: datetime | None = None
        self._started_before_open = False
        self._degraded_reason: str | None = None
        self._started = False

    @property
    def feed(self) -> LseTickFeed | None:
        return self._feed

    @property
    def latest_tick(self) -> LseTick | None:
        """The most recent WebSocket tick, for the execution adapter's quote."""
        return self._feed.last_tick if self._feed is not None else None

    @property
    def api_key(self) -> str:
        if self._api_key is None:
            self._api_key = load_api_key(self.key_file)
        return self._api_key

    @property
    def archive(self) -> LseArchive:
        if self._archive is None:
            self._archive = LseArchive(self.api_key)
        return self._archive

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    @property
    def started_before_open(self) -> bool:
        return self._started_before_open

    def _on_bar(self, bar: LseBar) -> None:
        if self._cache is not None:
            self._cache.append(bar)
        log.info(
            "LSE BAR %s o=%.2f h=%.2f l=%.2f c=%.2f v=%.0f trades=%d",
            bar.time,
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.trades,
        )

    def _on_disconnect(self, reason: str) -> None:
        now = datetime.now(tz=NY_TZ)
        if _feed_protected(now):
            self._degraded_reason = (
                f"{reason} during the protected session; missing ticks cannot "
                "be reconstructed intraday"
            )
            log.critical("LSE SESSION DEGRADED: %s", self._degraded_reason)

    def start(self, *, now: datetime | None = None, timeout: float = 30.0) -> None:
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

        self._warmup = self.archive.warmup(
            self.symbol,
            days=self.warmup_days,
            cache_dir=self.cache_root,
            now=current,
        )
        self._cache = LiveBarCache(self.cache_root / "live", self.symbol)
        self._loaded_today = self._cache.load(current.date())
        self._builder = FiveMinuteBarBuilder(
            self.symbol,
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
            "LSE source ready %s | warmup=%d bars through %s | today_cache=%d bars | %s",
            self.symbol,
            len(self._warmup),
            self._warmup.index.max().date() if not self._warmup.empty else "?",
            len(self._loaded_today),
            self._feed.status(),
        )

    def poll(self, now: datetime | None = None) -> list[LseBar]:
        if self._builder is None or self._feed is None:
            return []
        current = _local(now or datetime.now(tz=NY_TZ))
        if current.date() != self._builder.active_date:
            self._builder.set_active_date(current.date())
        if self._feed.fatal:
            self._degraded_reason = (
                f"LSE feed stopped: {self._feed.error or 'unknown fatal error'}"
            )
        elif _feed_protected(current) and not self._feed.ready:
            self._degraded_reason = (
                "LSE WebSocket is not subscribed during the protected session"
            )
        finished = self._builder.finalize(current, self.bar_grace_seconds)
        if self._builder.gap_reason and self._degraded_reason is None:
            self._degraded_reason = self._builder.gap_reason
            log.critical("LSE SESSION DEGRADED: %s", self._degraded_reason)
        return finished

    def frame(self) -> pd.DataFrame:
        parts = [self._warmup, self._loaded_today]
        if self._builder is not None:
            parts.append(self._builder.frame())
        pieces = [part for part in parts if not part.empty]
        if not pieces:
            return pd.DataFrame(
                columns=list(_BAR_COLUMNS),
                dtype=float,
                index=pd.DatetimeIndex([], name="time"),
            )
        frame = pd.concat(pieces)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        return frame.loc[:, list(_BAR_COLUMNS)].astype(float)

    @property
    def today_bars(self) -> int:
        if self._builder is None:
            return 0
        frame = self._builder.frame()
        if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
            return 0
        today = datetime.now(tz=NY_TZ).date()
        return int((frame.index.date == today).sum())

    def status(self) -> str:
        feed = self._feed.status() if self._feed is not None else "LSE not started"
        warmup = (
            f"{len(self._warmup)} bars through {self._warmup.index.max().date()}"
            if not self._warmup.empty
            else "no warmup"
        )
        today = self.today_bars
        return (
            f"{warmup} | today={today} bars | started_before_open="
            f"{self._started_before_open} | {feed}"
            + (
                f" | DEGRADED: {self._degraded_reason}"
                if self._degraded_reason
                else ""
            )
        )

    def stop(self) -> None:
        if self._feed is not None:
            self._feed.stop()
            self._feed = None
        self._builder = None
        self._started = False


SHARED_STATUS_SCHEMA = 1
SHARED_STATUS_STALE_SECONDS = 15.0


def shared_status_path(
    cache_dir: str | Path | None, symbol: str
) -> Path:
    """The one status file the shared LSE bridge publishes."""
    root = (
        Path(cache_dir)
        if cache_dir
        else Path(__file__).resolve().parents[1] / "data" / "lse"
    )
    return root / "shared" / f"{symbol.upper()}_status.json"


class SharedLseSignalSource:
    """Read the single LSE bridge's warmup, live bars and heartbeat.

    Only the bridge opens the LSE WebSocket.  Any number of executors can use
    this source at the same time: they read the bridge's warmup parquet, the
    append-only live cache and its atomic status file.  The bridge is the only
    process that needs the LSE key or network connection.
    """

    def __init__(
        self,
        symbol: str,
        *,
        cache_dir: str | Path | None = None,
        status_timeout: float = SHARED_STATUS_STALE_SECONDS,
    ) -> None:
        self.symbol = symbol.upper()
        self.cache_root = (
            Path(cache_dir)
            if cache_dir
            else Path(__file__).resolve().parents[1] / "data" / "lse"
        )
        self.status_path = shared_status_path(self.cache_root, self.symbol)
        self.warmup_path = self.cache_root / f"{self.symbol}_5m_warmup.parquet"
        self._live = LiveBarCache(self.cache_root / "live", self.symbol)
        self._warmup = pd.DataFrame(
            columns=list(_BAR_COLUMNS), dtype=float
        )
        self._today = pd.DataFrame(columns=list(_BAR_COLUMNS), dtype=float)
        self._status: dict[str, object] = {}
        self._started = False
        self._started_before_open = False
        self._degraded_reason: str | None = None
        self._status_timeout = max(1.0, float(status_timeout))
        self._status_age = float("inf")
        # The runner's logical clock, not the wall clock, keeps quote-age
        # checks deterministic in tests and makes every poll use the same
        # instant as its status read.
        self._clock = datetime.now(tz=NY_TZ)

    @property
    def degraded_reason(self) -> str | None:
        return self._degraded_reason

    @property
    def started_before_open(self) -> bool:
        return self._started_before_open

    @property
    def latest_tick(self) -> LseTick | None:
        """The bridge's latest tick, used for an LSE-sourced quote."""
        payload = self._status.get("last_tick")
        if not isinstance(payload, dict):
            return None
        try:
            stamp = parse_timestamp(payload.get("timestamp"))
            age = (self._clock - stamp.astimezone(NY_TZ)).total_seconds()
            if age > max(30.0, self._status_timeout * 2):
                return None
            price = float(payload.get("price") or 0.0)
            if price <= 0:
                return None
            bid = payload.get("bid")
            ask = payload.get("ask")
            return LseTick(
                symbol=str(payload.get("symbol") or self.symbol),
                price=price,
                volume=float(payload.get("volume") or 0.0),
                timestamp=stamp,
                bid=float(bid) if bid is not None else None,
                ask=float(ask) if ask is not None else None,
            )
        except (TypeError, ValueError, KeyError):
            return None

    def _read_status(self, now: datetime) -> dict[str, object] | None:
        self._status_age = float("inf")
        if not self.status_path.is_file():
            return None
        try:
            from src.file_io import read_text_shared

            payload = json.loads(read_text_shared(self.status_path))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        updated = payload.get("updated_at")
        try:
            stamp = datetime.fromisoformat(str(updated))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=NY_TZ)
            age = (_local(now) - stamp.astimezone(NY_TZ)).total_seconds()
        except (TypeError, ValueError):
            age = self._status_timeout + 1.0
        self._status_age = age
        if age > self._status_timeout:
            self._degraded_reason = (
                f"shared LSE bridge heartbeat is stale ({age:.0f}s old); "
                "the bridge may have stopped"
            )
        return payload

    def _load_warmup(self, status: dict[str, object]) -> pd.DataFrame:
        raw_path = status.get("warmup_cache")
        path = Path(str(raw_path)) if raw_path else self.warmup_path
        if not path.is_file():
            raise LseFeedError(
                f"shared LSE warmup cache is missing at {path}; "
                "start the shared LSE bridge first"
            )
        try:
            frame = pd.read_parquet(path)
            frame = _normalise_ohlcv(frame)
        except Exception as exc:
            raise LseFeedError(
                f"shared LSE warmup cache is unreadable at {path}: {exc}"
            ) from exc
        if frame.empty:
            raise LseFeedError("shared LSE warmup cache has no completed RTH bars")
        return frame

    def start(
        self, *, now: datetime | None = None, timeout: float = 45.0
    ) -> None:
        if self._started:
            return
        current = _local(now or datetime.now(tz=NY_TZ))
        self._clock = current
        deadline = time_module.monotonic() + max(0.5, float(timeout))
        status: dict[str, object] | None = None
        while time_module.monotonic() < deadline:
            status = self._read_status(current)
            if status is not None:
                if status.get("fatal"):
                    raise LseFeedError(
                        f"shared LSE bridge is fatal: {status.get('error') or 'unknown error'}"
                    )
                if status.get("ready"):
                    break
            time_module.sleep(0.25)
        if status is None:
            raise LseFeedError(
                f"shared LSE bridge is not publishing {self.status_path}; "
                "start it before the executor"
            )
        if not status.get("ready"):
            raise LseFeedError(
                f"shared LSE bridge did not become ready: {status.get('error') or 'timeout'}"
            )
        if self._status_age > self._status_timeout:
            raise LseFeedError(
                "shared LSE bridge status is stale; restart the shared LSE bridge"
            )
        session_date = str(status.get("session_date") or "")
        if session_date and session_date != current.date().isoformat():
            raise LseFeedError(
                f"shared LSE bridge is for {session_date}, not {current.date().isoformat()}; "
                "restart the bridge for today"
            )
        self._status = status
        self._warmup = self._load_warmup(status)
        self._today = self._live.load(current.date())
        self._started_before_open = bool(status.get("started_before_open"))
        self._degraded_reason = (
            str(status.get("degraded_reason"))
            if status.get("degraded_reason")
            else None
        )
        if (
            not self._started_before_open
            and current.weekday() < 5
            and current.time() >= RTH_OPEN
            and self._degraded_reason is None
        ):
            self._degraded_reason = (
                "shared LSE bridge started at or after 09:30 ET; the opening "
                "bars cannot be reconstructed"
            )
        self._started = True
        log.info(
            "SHARED LSE SOURCE ready %s | warmup=%d bars through %s | "
            "today_cache=%d bars | status=%s",
            self.symbol,
            len(self._warmup),
            self._warmup.index.max().date() if not self._warmup.empty else "?",
            len(self._today),
            self.status(),
        )

    def poll(self, now: datetime | None = None) -> list[LseBar]:
        if not self._started:
            return []
        current = _local(now or datetime.now(tz=NY_TZ))
        self._clock = current
        status = self._read_status(current)
        if status is None:
            self._degraded_reason = (
                "shared LSE bridge status is missing; the executor cannot "
                "verify the live feed"
            )
            return []
        if status.get("fatal"):
            self._degraded_reason = (
                f"shared LSE bridge stopped: {status.get('error') or 'unknown error'}"
            )
            return []
        session_date = str(status.get("session_date") or "")
        if session_date and session_date != current.date().isoformat():
            self._degraded_reason = (
                f"shared LSE bridge is for {session_date}, not "
                f"{current.date().isoformat()}; restart the bridge"
            )
            return []
        if status.get("degraded_reason"):
            self._degraded_reason = str(status.get("degraded_reason"))
        self._status = status
        try:
            self._today = self._live.load(current.date())
        except Exception as exc:
            self._degraded_reason = f"shared LSE live cache is unreadable: {exc}"
        return []

    def frame(self) -> pd.DataFrame:
        pieces = [part for part in (self._warmup, self._today) if not part.empty]
        if not pieces:
            return pd.DataFrame(
                columns=list(_BAR_COLUMNS),
                dtype=float,
                index=pd.DatetimeIndex([], name="time"),
            )
        frame = pd.concat(pieces)
        frame = frame[~frame.index.duplicated(keep="last")].sort_index()
        return frame.loc[:, list(_BAR_COLUMNS)].astype(float)

    @property
    def today_bars(self) -> int:
        if self._today.empty:
            return 0
        return int((self._today.index.date == datetime.now(tz=NY_TZ).date()).sum())

    def status(self) -> str:
        feed = str(self._status.get("feed_status") or "shared LSE bridge")
        heartbeat = str(self._status.get("updated_at") or "?")
        return (
            f"shared LSE | warmup={len(self._warmup)} bars | "
            f"today={self.today_bars} bars | heartbeat={heartbeat} | {feed}"
            + (
                f" | DEGRADED: {self._degraded_reason}"
                if self._degraded_reason
                else ""
            )
        )

    def stop(self) -> None:
        self._started = False


def create_signal_source(
    symbol: str,
    *,
    feed_mode: str = "direct",
    key_file: str | Path | None = None,
    warmup_days: int = DEFAULT_WARMUP_DAYS,
    cache_dir: str | Path | None = None,
    bar_grace_seconds: float = DEFAULT_BAR_GRACE_SECONDS,
) -> LseSignalSource | SharedLseSignalSource:
    """Create the direct socket source or the shared-bridge reader."""
    if feed_mode == "shared":
        return SharedLseSignalSource(symbol, cache_dir=cache_dir)
    if feed_mode != "direct":
        raise ValueError(f"unknown LSE feed mode: {feed_mode!r}")
    return LseSignalSource(
        symbol,
        key_file=key_file,
        warmup_days=warmup_days,
        cache_dir=cache_dir,
        bar_grace_seconds=bar_grace_seconds,
    )


def bar_frame(bars: Iterable[LseBar]) -> pd.DataFrame:
    """Build an OHLCV frame from completed bars, for tests and verifiers."""
    rows = list(bars)
    if not rows:
        return pd.DataFrame(columns=list(_BAR_COLUMNS), dtype=float)
    return pd.DataFrame(
        [bar.as_row() for bar in rows],
        index=pd.DatetimeIndex([bar.time for bar in rows], name="time"),
    ).sort_index().astype(float)
