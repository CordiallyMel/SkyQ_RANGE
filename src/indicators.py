"""OHLCV-only features for the SkyQ RANGE channel scalper.

The Pine source needs five derived series:

* the rolling high/low normalization of close over ``NORM_LENGTH`` bars;
* the rolling standard deviation of that normalized price over ``BOX_LENGTH``;
* the relative position of the highest and lowest volatility inside a
  ``BOX_LENGTH + 1`` window (Pine's ``ta.highestbars`` / ``ta.lowestbars``);
* the bars since the low-volatility crossover (Pine's ``ta.barssince``);
* the highest high and lowest low over that duration.

The channel is *created* on the opposite crossover, using the running
high/low from the low-volatility crossover through the creation bar.  Every
value at bar ``i`` uses only bars ``0..i``; the executable entry is the next
bar's open.  There is no look-ahead in the channel definition.

The parquet ADX/Bollinger/VWAP columns are ignored.  ATR is computed from
OHLCV and is used only for the optional volatility filter and for reporting.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from config.settings import (
    ATR_PERIOD,
    BOX_LENGTH,
    DETECT_WINDOW,
    EVENT_CALENDAR_FILE,
    MIN_CHANNEL_BARS,
    NORM_LENGTH,
    NY_TZ,
    SESSION_CHOICES,
    bar_minutes,
)

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
FEATURE_COLUMNS = (
    "norm",
    "vol",
    "upper",
    "lower",
    "duration",
    "channel_event",
    "channel_top",
    "channel_bottom",
    "atr",
    "bar_close",
)


def _ny_index(values: object) -> pd.DatetimeIndex:
    index = values if isinstance(values, pd.DatetimeIndex) else pd.DatetimeIndex(values)
    if index.tz is None:
        return index.tz_localize(NY_TZ)
    return index.tz_convert(NY_TZ)


def read_bars(path: str | Path, *, columns: Iterable[str] | None = None) -> pd.DataFrame:
    """Read a parquet or MetaTrader-style CSV OHLCV export.

    Parquet files are already New-York indexed.  CSV exports such as
    ``US500m_PERIOD_M1_OHLC.csv`` carry a naive ``Timestamp`` in UTC, so the
    column is parsed as UTC and converted to New York before the strategy sees
    it.  Mapping that header to lowercase OHLCV keeps the same loader usable for
    both formats.
    """
    source = Path(path)
    if source.suffix.lower() != ".csv":
        if columns is None:
            return pd.read_parquet(source)
        return pd.read_parquet(source, columns=list(columns))

    raw = pd.read_csv(source)
    raw.columns = [str(column).strip().lower() for column in raw.columns]
    raw = raw.rename(
        columns={
            "timestamp": "time",
            "tickvolume": "volume",
            "tick_volume": "volume",
            "tick volume": "volume",
        }
    )
    if "time" not in raw.columns:
        raise ValueError(f"CSV {source} needs a Timestamp/time column")
    raw["time"] = pd.to_datetime(raw["time"], format="mixed", utc=True)
    raw = raw.set_index("time").sort_index()
    raw.index = raw.index.tz_convert(NY_TZ)
    return raw


def normalise_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted, unique, New-York-indexed OHLCV frame."""
    if frame.empty:
        return pd.DataFrame(columns=list(OHLCV_COLUMNS), dtype=float)

    data = frame.copy()
    data.columns = [str(column).strip().lower() for column in data.columns]
    data = data.rename(
        columns={
            "timestamp": "time",
            "tickvolume": "volume",
            "tick_volume": "volume",
            "tick volume": "volume",
        }
    )
    if not isinstance(data.index, pd.DatetimeIndex):
        for column in ("time", "timestamp", "date", "datetime"):
            if column in data.columns:
                data = data.set_index(column)
                break
    missing = [column for column in OHLCV_COLUMNS if column not in data.columns]
    if missing:
        raise ValueError(f"bars are missing columns: {', '.join(missing)}")

    data.index = _ny_index(data.index)
    data = data[~data.index.duplicated(keep="last")].sort_index()
    return data.loc[:, list(OHLCV_COLUMNS)].astype(float)


def resample_bars(frame: pd.DataFrame, timeframe: str = "5m") -> pd.DataFrame:
    """Aggregate OHLCV to ``timeframe`` using left-labelled, left-closed bars."""
    data = normalise_bars(frame)
    if data.empty:
        return data
    minutes = bar_minutes(timeframe)
    if minutes <= 1:
        return data
    bars = data.resample(f"{minutes}min", label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    return bars.dropna(subset=["open", "high", "low", "close"])


def wilder_atr(frame: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder's Average True Range, seeded from the first ``period`` ranges."""
    if period < 1:
        raise ValueError("ATR period must be positive")
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    close = frame["close"].to_numpy(dtype=float)
    if high.size == 0:
        return pd.Series(dtype=float, index=frame.index)
    previous_close = np.concatenate(([np.nan], close[:-1]))
    true_range = np.nanmax(
        np.vstack(
            [
                high - low,
                np.abs(high - previous_close),
                np.abs(low - previous_close),
            ]
        ),
        axis=0,
    )
    true_range[0] = high[0] - low[0]
    output = np.full(true_range.size, np.nan)
    if true_range.size < period:
        return pd.Series(output, index=frame.index)
    average = float(np.mean(true_range[:period]))
    output[period - 1] = average
    for index in range(period, true_range.size):
        average = (average * (period - 1) + true_range[index]) / period
        output[index] = average
    return pd.Series(output, index=frame.index)


def _rolling_extreme_position(values: np.ndarray, window: int, mode: str) -> np.ndarray:
    """Relative position (0 oldest .. 1 newest) of the rolling max/min.

    Pine's ``ta.highestbars(source, length)`` returns a *negative* offset; the
    source converts it with ``(offset + length) / length``.  For a window of
    ``length + 1`` bars that is exactly ``argmax / length``.  We compute the
    same quantity in chunks so a 2.6-million-bar 1-minute series does not need
    a 300 MB temporary array.
    """
    values = np.asarray(values, dtype=float)
    output = np.full(values.size, np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size == 0:
        return output
    start = int(valid[0])
    window_values = values[start:]
    if window_values.size < window:
        return output
    span = window_values.size - window + 1
    chunk = 250_000
    for offset in range(0, span, chunk):
        stop = min(offset + chunk, span)
        sliced = window_values[offset : stop + window - 1]
        windows = sliding_window_view(sliced, window)
        # Pine's ta.highestbars/ta.lowestbars resolve ties to the most recent
        # occurrence.  argmax on the reversed window gives that offset.
        if mode == "max":
            positions = window - 1 - windows[:, ::-1].argmax(axis=1)
        else:
            positions = window - 1 - windows[:, ::-1].argmin(axis=1)
        output[start + window - 1 + offset : start + window - 1 + stop] = (
            positions / float(window - 1)
        )
    return output


def add_features(
    frame: pd.DataFrame,
    *,
    timeframe: str = "5m",
    norm_length: int = NORM_LENGTH,
    box_length: int = BOX_LENGTH,
    min_channel_bars: int = MIN_CHANNEL_BARS,
    atr_period: int = ATR_PERIOD,
    atr_1m: pd.Series | None = None,
) -> pd.DataFrame:
    """Add the SkyQ RANGE channel features and a Wilder ATR to an OHLCV frame."""
    data = normalise_bars(frame)
    if data.empty:
        return data

    high = data["high"].to_numpy(dtype=float)
    low = data["low"].to_numpy(dtype=float)
    close = data["close"].to_numpy(dtype=float)
    count = close.size

    rolling_low = data["low"].rolling(norm_length, min_periods=norm_length).min()
    rolling_high = data["high"].rolling(norm_length, min_periods=norm_length).max()
    low_array = rolling_low.to_numpy(dtype=float)
    high_array = rolling_high.to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        normalized = (close - low_array) / (high_array - low_array)
    normalized = np.where(np.isfinite(normalized), normalized, np.nan)

    volatility = (
        pd.Series(normalized)
        .rolling(box_length, min_periods=box_length)
        .std(ddof=0)
        .to_numpy(dtype=float)
    )

    # Pine: upper = (highestbars(vol, length+1) + length) / length
    #       lower = (lowestbars(vol, length+1) + length) / length
    window = box_length + 1
    upper = _rolling_extreme_position(volatility, window, "max")
    lower = _rolling_extreme_position(volatility, window, "min")

    previous_upper = np.concatenate(([np.nan], upper[:-1]))
    previous_lower = np.concatenate(([np.nan], lower[:-1]))
    low_cross_up = (lower > upper) & (previous_lower <= previous_upper)
    high_cross_up = (upper > lower) & (previous_upper <= previous_lower)

    indices = np.arange(count, dtype=float)
    last_cross = pd.Series(np.where(low_cross_up, indices, np.nan)).ffill().to_numpy()
    # Pine's ta.barssince returns 0 on the crossover bar, so the duration at a
    # later bar is index - crossover index, not +1.  The channel high/low then
    # starts on the bar *after* the low-volatility crossover.
    duration = np.where(
        np.isnan(last_cross),
        1.0,
        np.maximum(indices - last_cross, 1.0),
    )

    segment = np.cumsum(low_cross_up.astype(np.int64))
    segment_series = pd.Series(segment)
    high_series = pd.Series(np.where(low_cross_up, np.nan, high))
    low_series = pd.Series(np.where(low_cross_up, np.nan, low))
    running_top = high_series.groupby(segment_series).cummax().to_numpy(dtype=float)
    running_bottom = low_series.groupby(segment_series).cummin().to_numpy(dtype=float)

    channel_event = high_cross_up & (duration > min_channel_bars)
    channel_top = np.where(channel_event, running_top, np.nan)
    channel_bottom = np.where(channel_event, running_bottom, np.nan)

    data["norm"] = normalized
    data["vol"] = volatility
    data["upper"] = upper
    data["lower"] = lower
    data["duration"] = duration
    data["channel_event"] = channel_event
    data["channel_top"] = channel_top
    data["channel_bottom"] = channel_bottom
    data["atr"] = wilder_atr(data, atr_period)
    data["bar_close"] = data.index + pd.Timedelta(minutes=bar_minutes(timeframe))

    if atr_1m is not None and not atr_1m.empty:
        reference = pd.Series(
            atr_1m.to_numpy(dtype=float),
            index=_ny_index(atr_1m.index),
        )
        reference = reference[~reference.index.duplicated(keep="last")].sort_index()
        closes = pd.DataFrame({"bar_close": data["bar_close"].to_numpy()})
        closes.index = data.index
        aligned = pd.merge_asof(
            closes.sort_values("bar_close"),
            pd.DataFrame({"bar_close": reference.index, "atr_1m": reference.to_numpy()}),
            on="bar_close",
            direction="backward",
            allow_exact_matches=True,
        )
        aligned.index = closes.sort_values("bar_close").index
        data["atr_1m"] = aligned["atr_1m"].to_numpy()
    else:
        data["atr_1m"] = data["atr"]
    return data


def gate_ready(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop bars whose channel or ATR features are not ready."""
    required = ["channel_event", "atr"]
    if "atr_1m" in frame.columns:
        required.append("atr_1m")
    return frame.dropna(subset=required).copy()


def _minutes(index: pd.DatetimeIndex) -> np.ndarray:
    return (index.hour * 60 + index.minute).to_numpy(dtype=int)


def session_mask(index: pd.DatetimeIndex, session: str = "all") -> np.ndarray:
    """Boolean mask for a named session, in New York time.

    * ``all``     - every bar.
    * ``rth``     - 09:30-15:59:59 ET.
    * ``core``    - 10:00-14:59:59 ET.
    * ``evening`` - 18:00-00:59:59 ET.
    """
    session = session.strip().lower()
    if session not in SESSION_CHOICES:
        raise ValueError(f"session must be one of {', '.join(SESSION_CHOICES)}")
    ny = _ny_index(index)
    minutes = _minutes(ny)
    if session == "all":
        return np.ones(len(ny), dtype=bool)
    if session == "rth":
        return (minutes >= 9 * 60 + 30) & (minutes < 16 * 60)
    if session == "core":
        return (minutes >= 10 * 60) & (minutes < 15 * 60)
    return (minutes >= 18 * 60) | (minutes < 60)


def session_allows(timestamp: object, session: str = "all") -> bool:
    return bool(session_mask(pd.DatetimeIndex([pd.Timestamp(timestamp)]), session)[0])


def skip_hours_mask(index: pd.DatetimeIndex, skip_hours: tuple[int, ...] = ()) -> np.ndarray:
    """True for bars whose hour is not in ``skip_hours``."""
    ny = _ny_index(index)
    if not skip_hours:
        return np.ones(len(ny), dtype=bool)
    hours = {int(hour) for hour in skip_hours}
    return ~np.isin(ny.hour.to_numpy(dtype=int), list(hours))


def load_event_calendar(path: str | Path = EVENT_CALENDAR_FILE) -> pd.DataFrame:
    """Load the wide macro calendar, indexed by date."""
    calendar_path = Path(path)
    if not calendar_path.exists():
        log.warning("event calendar %s is missing; event filter disabled", calendar_path)
        return pd.DataFrame()
    frame = pd.read_csv(calendar_path)
    if "date" not in frame.columns:
        raise ValueError(f"{calendar_path} is missing the 'date' column")
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame = frame.drop_duplicates(subset="date", keep="last").set_index("date")
    return frame


def event_on(
    date: object,
    calendar: pd.DataFrame,
    codes: tuple[str, ...] = (),
) -> bool:
    """Return True when ``date`` has any of the requested event flags."""
    if not codes or calendar.empty:
        return False
    day = pd.Timestamp(date).date()
    if day not in calendar.index:
        return False
    row = calendar.loc[day]
    if isinstance(row, pd.DataFrame):
        row = row.iloc[-1]
    for code in codes:
        if code in row.index and int(row[code]) == 1:
            return True
    return False


def load_raw_1m(
    path: str | Path,
    *,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """Read a 1-minute parquet or CSV, optionally sliced (UTC-safe, NY-local)."""
    frame = normalise_bars(read_bars(path, columns=list(OHLCV_COLUMNS)))
    if start is not None:
        start_ts = pd.Timestamp(start)
        start_ts = start_ts.tz_localize(NY_TZ) if start_ts.tzinfo is None else start_ts.tz_convert(NY_TZ)
        frame = frame[frame.index >= start_ts]
    if end is not None:
        end_ts = pd.Timestamp(end)
        end_ts = end_ts.tz_localize(NY_TZ) if end_ts.tzinfo is None else end_ts.tz_convert(NY_TZ)
        frame = frame[frame.index <= end_ts]
    return frame


def load_bars(
    path: str | Path,
    *,
    timeframe: str = "5m",
    start: str | None = None,
    end: str | None = None,
    norm_length: int = NORM_LENGTH,
    box_length: int = BOX_LENGTH,
    min_channel_bars: int = MIN_CHANNEL_BARS,
    atr_period: int = ATR_PERIOD,
    with_1m_atr: bool = True,
) -> pd.DataFrame:
    """Read a 1-minute parquet or CSV, resample, and add SkyQ RANGE features.

    Features are computed on the full history before the requested date slice,
    so the first returned bar already has a valid 100-bar normalization and a
    valid 14-bar volatility window.  When ``timeframe`` is not 1m, the 1-minute
    ATR series is built first and aligned to each higher-timeframe close, which
    reproduces Pine's ``request.security(..., "1", ta.atr(14))``.
    """
    raw = normalise_bars(read_bars(path, columns=list(OHLCV_COLUMNS)))
    if raw.empty:
        return raw
    bars = resample_bars(raw, timeframe)
    one_minute_atr = None
    if with_1m_atr and bar_minutes(timeframe) > 1:
        one_minute_atr = wilder_atr(raw, atr_period)
    frame = add_features(
        bars,
        timeframe=timeframe,
        norm_length=norm_length,
        box_length=box_length,
        min_channel_bars=min_channel_bars,
        atr_period=atr_period,
        atr_1m=one_minute_atr,
    )
    if start is not None:
        start_ts = pd.Timestamp(start)
        start_ts = start_ts.tz_localize(NY_TZ) if start_ts.tzinfo is None else start_ts.tz_convert(NY_TZ)
        frame = frame[frame.index >= start_ts]
    if end is not None:
        end_ts = pd.Timestamp(end)
        end_ts = end_ts.tz_localize(NY_TZ) if end_ts.tzinfo is None else end_ts.tz_convert(NY_TZ)
        frame = frame[frame.index <= end_ts + pd.Timedelta(days=1)]
    return frame


def completed(frame: pd.DataFrame, now: datetime, minutes: int = 5) -> pd.DataFrame:
    """Return bars that have fully closed at or before ``now``.

    The parquet and IBKR intraday indexes are bar *start* times, so the bar
    stamped 10:15 is still forming until 10:20 on a 5-minute chart.
    """
    if frame.empty:
        return frame.copy()
    cutoff = pd.Timestamp(now)
    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize(NY_TZ)
    else:
        cutoff = cutoff.tz_convert(NY_TZ)
    return frame[frame.index <= cutoff - pd.Timedelta(minutes=minutes)].copy()
