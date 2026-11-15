"""Configuration for the SkyQ RANGE channel-scalper.

The strategy is a mean-reversion scalper.  It builds a consolidation channel
from a normalized-price volatility measure, then buys the lower 20% of the
channel and sells the upper 20%, targeting a fraction of the channel and
stopping just outside it.  SPY and QQQ each have their own stop, channel-width
floor and time stop.

Everything is derived from OHLCV.  The parquet ADX/Bollinger columns are never
consumed.  The only non-OHLCV input is the optional daily macro-event calendar.

Research defaults live in this module; ``scripts.backtest_range`` and
``scripts.research_futures`` are the supported reproduction paths.  See
``README.md`` and ``RANGE_REPORT.md`` for the current study.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")

# --- channel inputs ----------------------------------------------------------
# Pine: length_ = 100 (normalization lookback), length = 14 (box detection).
NORM_LENGTH = 100
BOX_LENGTH = 14
# Pine's ta.highestbars / ta.lowestbars window is length + 1.
DETECT_WINDOW = BOX_LENGTH + 1
# A channel needs at least this many bars since the low-volatility crossover.
MIN_CHANNEL_BARS = 10
ATR_PERIOD = 14

# --- entry / exit inputs -----------------------------------------------------
# Fraction of the channel range treated as the entry zone at each edge.
# The full-history SPY scan prefers a wider 20% zone: it waits for a deeper
# mean-reversion touch and removes the marginal edge-of-zone entries.
ENTRY_ZONE = 0.20
QUANTITY = 1000
# Stop offset beyond the channel edge, in ticks (Pine default 5, published 50).
# 100 SPY ticks ($1.00) is the stable full-history choice; a tighter stop turns
# the high win rate into a losing expectancy after costs.
STOP_TICKS = 100.0
# Target: fraction of the channel range, measured from the entry reference.
# 20% keeps the hit rate high; 75% has a better RR but a much lower hit rate.
TP_CHANNEL_PCT = 0.20
TP_MODE = "channel"  # "channel" or "fixed"
TP_FIXED_TICKS = 30.0
# Minimum bars between a long signal and the next long signal (independent for
# shorts).  Zero is the full-history optimum and is also better than 3 in both
# sub-periods and drawdown; the runner is naturally spaced by one completed bar
# per poll, so this does not create duplicate same-bar entries.
COOLDOWN_BARS = 0
# Allow more than one overlapping channel to be tracked at once.
OVERLAP = True
# Pine removes a channel when a close is more than this many ticks outside it.
BREAKOUT_TICKS = 10.0

# --- filters -----------------------------------------------------------------
# ATR filter: the Pine default is off, the published run is on with a 1-minute
# ATR(14) >= 4.0 on NQ.  The threshold is instrument-specific, so research uses
# a percentile-scaled version and the live default is off.
ATR_FILTER = False
ATR_MIN = 4.0
ATR_PERCENTILE = None  # e.g. 0.35 to use a rolling percentile instead

# The published NQ scalper is a 1-minute, all-hours strategy.  Both ETF books
# trade the regular-hours session; the futures are negative after costs.
# A fixed dollar floor becomes looser as SPY rises, so the live default is a
# price-relative floor.  0.20% of the entry price keeps the same selectivity
# from SPY ~$290 (2019) through SPY ~$760 (2026).
MIN_CHANNEL_WIDTH = 0.0
MIN_CHANNEL_PCT: float | None = 0.0020
MAX_CHANNEL_WIDTH: float | None = None

# The short mirror is positive on its own, but running both sides is not
# additive: one position at a time makes the sides compete.  Both is the live
# default because it uses the full signal set; set SKYQ_RANGE_SIDES=long for the
# lower-drawdown reference book.
SIDES = os.getenv(
    "SKYQ_RANGE_SIDES", os.getenv("BOCS_SIDES", "both")
).strip().lower()
SIDES_CHOICES = ("long", "short", "both")

SESSION_ALL = "all"
SESSION_RTH = "rth"
SESSION_CORE = "core"
SESSION_EVENING = "evening"
SESSION_CHOICES = (SESSION_ALL, SESSION_RTH, SESSION_CORE, SESSION_EVENING)
SESSION = os.getenv(
    "SKYQ_RANGE_SESSION", os.getenv("BOCS_SESSION", SESSION_RTH)
).strip().lower()

# Optional hour filters, applied to the decision bar.  Empty disables.
SKIP_HOURS: tuple[int, ...] = ()
# Optional daily macro-event flags ("nfp", "cpi", "fomc", "pce", ...).
SKIP_EVENT_CODES: tuple[str, ...] = ()
EVENT_CALENDAR_FILE = "data/macro_event_calendar_wide.csv"

# --- execution assumptions ---------------------------------------------------
# The entry is a market order in the source.  The backtest fills it at the next
# bar open and charges one tick of slippage.  The target is a resting limit and
# is not slipped; the stop and time exits are market orders and are slipped.
SLIPPAGE_TICKS = 1.0
ASSUME_STOP_FIRST = True
USE_INTRABAR = True
# A safety time stop.  0 disables it (source behaviour).  120 one-minute bars
# is the full-history compromise: it releases dead trades without turning the
# scalp into an overnight hold.
MAX_HOLD_BARS = 120
# Optional flat-at-session-end.  None disables.  Otherwise a New-York
# minute-of-day such as 15*60+55 (15:55 ET).
FLAT_AT_MINUTE: int | None = 15 * 60 + 55

# --- data / live -------------------------------------------------------------
BAR_MINUTES = 1
DEFAULT_SYMBOL = "SPY"
DEFAULT_UNIVERSE = ("NQ", "MNQ", "ES", "MES", "YM", "MYM", "RTY", "M2K", "CL", "MCL", "GC", "MGC")
MIN_HISTORY_BARS = NORM_LENGTH + DETECT_WINDOW + ATR_PERIOD + 5


# --- instruments -------------------------------------------------------------
@dataclass(frozen=True)
class Instrument:
    """One tradeable instrument and the parquet series used to research it."""

    symbol: str
    data_symbol: str
    exchange: str
    multiplier: float
    tick_size: float
    description: str
    asset_class: str = "FUT"
    commission_per_contract: float = 0.0
    commission_per_share: float = 0.0
    min_commission_per_order: float = 0.0

    @property
    def point_value(self) -> float:
        return self.multiplier

    @property
    def tick_value(self) -> float:
        return self.multiplier * self.tick_size

    @property
    def is_future(self) -> bool:
        return self.asset_class == "FUT"

    def data_file_1m(self) -> str:
        return f"data/{self.data_symbol}_1m.parquet"

    def data_file_for(self, timeframe: str = "1m") -> str:
        if timeframe == "1m":
            return self.data_file_1m()
        return f"data/{self.data_symbol}_{timeframe}.parquet"


INSTRUMENTS: dict[str, Instrument] = {
    "NQ": Instrument(
        symbol="NQ",
        data_symbol="nq",
        exchange="CME",
        multiplier=20.0,
        tick_size=0.25,
        description="E-mini Nasdaq-100",
        commission_per_contract=2.06,
    ),
    "MNQ": Instrument(
        symbol="MNQ",
        data_symbol="mnq",
        exchange="CME",
        multiplier=2.0,
        tick_size=0.25,
        description="Micro E-mini Nasdaq-100",
        commission_per_contract=0.62,
    ),
    "ES": Instrument(
        symbol="ES",
        data_symbol="es",
        exchange="CME",
        multiplier=50.0,
        tick_size=0.25,
        description="E-mini S&P 500",
        commission_per_contract=2.06,
    ),
    "MES": Instrument(
        symbol="MES",
        data_symbol="mes",
        exchange="CME",
        multiplier=5.0,
        tick_size=0.25,
        description="Micro E-mini S&P 500",
        commission_per_contract=0.62,
    ),
    "YM": Instrument(
        symbol="YM",
        data_symbol="ym",
        exchange="CBOT",
        multiplier=5.0,
        tick_size=1.0,
        description="E-mini Dow",
        commission_per_contract=2.06,
    ),
    "MYM": Instrument(
        symbol="MYM",
        data_symbol="ym",
        exchange="CBOT",
        multiplier=0.5,
        tick_size=1.0,
        description="Micro E-mini Dow; YM data proxy",
        commission_per_contract=0.62,
    ),
    "RTY": Instrument(
        symbol="RTY",
        data_symbol="rty",
        exchange="CME",
        multiplier=50.0,
        tick_size=0.10,
        description="E-mini Russell 2000",
        commission_per_contract=2.06,
    ),
    "M2K": Instrument(
        symbol="M2K",
        data_symbol="rty",
        exchange="CME",
        multiplier=5.0,
        tick_size=0.10,
        description="Micro E-mini Russell 2000; RTY data proxy",
        commission_per_contract=0.62,
    ),
    "CL": Instrument(
        symbol="CL",
        data_symbol="cl",
        exchange="NYMEX",
        multiplier=1000.0,
        tick_size=0.01,
        description="WTI Crude",
        commission_per_contract=2.06,
    ),
    "MCL": Instrument(
        symbol="MCL",
        data_symbol="cl",
        exchange="NYMEX",
        multiplier=100.0,
        tick_size=0.01,
        description="Micro WTI Crude; CL data proxy",
        commission_per_contract=0.62,
    ),
    "GC": Instrument(
        symbol="GC",
        data_symbol="gc",
        exchange="COMEX",
        multiplier=100.0,
        tick_size=0.10,
        description="Gold",
        commission_per_contract=2.06,
    ),
    "MGC": Instrument(
        symbol="MGC",
        data_symbol="gc",
        exchange="COMEX",
        multiplier=10.0,
        tick_size=0.10,
        description="Micro Gold; GC data proxy",
        commission_per_contract=0.62,
    ),
    "ZN": Instrument(
        symbol="ZN",
        data_symbol="zn",
        exchange="CBOT",
        multiplier=1000.0,
        tick_size=0.015625,
        description="10-Year T-Note",
        commission_per_contract=2.06,
    ),
    "ZT": Instrument(
        symbol="ZT",
        data_symbol="zt",
        exchange="CBOT",
        multiplier=2000.0,
        tick_size=0.00390625,
        description="2-Year T-Note",
        commission_per_contract=2.06,
    ),
    "SPY": Instrument(
        symbol="SPY",
        data_symbol="SPY",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.01,
        description="SPDR S&P 500 ETF",
        asset_class="STOCK",
        commission_per_share=0.005,
        min_commission_per_order=1.0,
    ),
    "QQQ": Instrument(
        symbol="QQQ",
        data_symbol="QQQ",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.01,
        description="Invesco QQQ Trust",
        asset_class="STOCK",
        commission_per_share=0.005,
        min_commission_per_order=1.0,
    ),
    "SPYM": Instrument(
        symbol="SPYM",
        data_symbol="SPYM",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.01,
        description="SPDR S&P 500 ETF (new feed)",
        asset_class="STOCK",
        commission_per_share=0.0035,
        min_commission_per_order=0.35,
    ),
    # Index CFDs from the MT5 bundle.  One unit is $1 per index point.  The
    # spread is charged through the runner's slippage arguments.
    "US500": Instrument(
        symbol="US500",
        data_symbol="US500",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.01,
        description="US500 CFD (MT5 export)",
        asset_class="CFD",
    ),
    "US30": Instrument(
        symbol="US30",
        data_symbol="US30",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.1,
        description="US30 CFD (MT5 export)",
        asset_class="CFD",
    ),
    "USTEC": Instrument(
        symbol="USTEC",
        data_symbol="USTEC",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.01,
        description="USTEC CFD (MT5 export)",
        asset_class="CFD",
    ),
    "UK100": Instrument(
        symbol="UK100",
        data_symbol="UK100",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.01,
        description="UK100 CFD (MT5 export)",
        asset_class="CFD",
    ),
    "STOXX50": Instrument(
        symbol="STOXX50",
        data_symbol="STOXX50",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.01,
        description="STOXX50 CFD (MT5 export)",
        asset_class="CFD",
    ),
    "JP225": Instrument(
        symbol="JP225",
        data_symbol="JP225",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.1,
        description="JP225 CFD (MT5 export)",
        asset_class="CFD",
    ),
    "AUS200": Instrument(
        symbol="AUS200",
        data_symbol="AUS200",
        exchange="SMART",
        multiplier=1.0,
        tick_size=0.01,
        description="AUS200 CFD (MT5 export)",
        asset_class="CFD",
    ),
}

DEFAULT_UNIVERSE = tuple(
    name for name, profile in INSTRUMENTS.items() if profile.asset_class != "CFD"
)
# The live book is ETF-only.  Futures remain available in the research harness
# so the negative cross-asset result can be reproduced, but they are blocked by
# ``src.main`` before any broker connection is opened.
LIVE_SYMBOLS = ("SPY", "QQQ")
RESEARCH_ONLY_SYMBOLS = tuple(
    name for name, profile in INSTRUMENTS.items() if profile.is_future
)


@dataclass(frozen=True)
class BookPreset:
    """One deployable ETF configuration.

    The two ETFs share the channel geometry but do not share a stop or a time
    stop.  Keeping those values in one preset prevents the old split-repository
    failure mode where an operator changed one copy and not the other.
    """

    symbol: str
    stop_ticks: float
    min_channel_pct: float
    max_hold_bars: int
    quantity: int = QUANTITY
    entry_zone: float = ENTRY_ZONE
    sides: str = SIDES
    session: str = SESSION
    flat_at_minute: int | None = FLAT_AT_MINUTE
    note: str = ""


BOOK_PRESETS: dict[str, BookPreset] = {
    "SPY": BookPreset(
        symbol="SPY",
        stop_ticks=100.0,
        min_channel_pct=0.0020,
        max_hold_bars=120,
        note="Full-history reference; 100 ticks ($1.00), 0.20% channel floor.",
    ),
    "QQQ": BookPreset(
        symbol="QQQ",
        stop_ticks=150.0,
        min_channel_pct=0.0040,
        max_hold_bars=240,
        note="Higher-volatility QQQ preset; 150 ticks ($1.50), 0.40% floor, 240 bars.",
    ),
}


def book_preset(symbol: str) -> BookPreset:
    """Return the deployable preset for an ETF symbol."""
    try:
        return BOOK_PRESETS[symbol.upper()]
    except KeyError as exc:
        choices = ", ".join(sorted(BOOK_PRESETS))
        raise KeyError(
            f"{symbol!r} has no deployable RANGE preset; live books are {choices}. "
            "Futures are research-only and are covered in reports/futures_gate.csv."
        ) from exc


@dataclass(frozen=True)
class RiskConfig:
    """Hard live-trading guards, evaluated before an order is submitted."""

    max_risk_pct: float = float(os.getenv("SKYQ_RANGE_MAX_RISK_PCT", "0.02"))
    max_notional_pct: float = float(os.getenv("SKYQ_RANGE_MAX_NOTIONAL_PCT", "1.00"))
    daily_loss_stop_pct: float = float(
        os.getenv("SKYQ_RANGE_DAILY_LOSS_STOP_PCT", "0.03")
    )

    def __post_init__(self) -> None:
        if not 0 < self.max_risk_pct <= 1:
            raise ValueError("max_risk_pct must be in (0, 1]")
        if not 0 < self.max_notional_pct <= 10:
            raise ValueError("max_notional_pct must be in (0, 10]")
        if not 0 <= self.daily_loss_stop_pct <= 1:
            raise ValueError("daily_loss_stop_pct must be in [0, 1]")


RISK = RiskConfig()


@dataclass(frozen=True)
class ConnectionConfig:
    """IB Gateway / TWS socket parameters.

    Ports: 4001 live Gateway, 4002 paper Gateway, 7496 live TWS, 7497 paper TWS.
    The default is the paper Gateway.
    """

    host: str = field(default_factory=lambda: os.getenv("IB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("IB_PORT", "4002")))
    client_id: int = field(default_factory=lambda: int(os.getenv("IB_CLIENT_ID", "46")))
    timeout: float = field(default_factory=lambda: float(os.getenv("IB_TIMEOUT", "10")))
    readonly: bool = field(
        default_factory=lambda: os.getenv("IB_READONLY", "0").lower()
        not in {"0", "false", "no"}
    )
    account: str | None = field(default_factory=lambda: os.getenv("IB_ACCOUNT") or None)


CONNECTION = ConnectionConfig()


def instrument(symbol: str) -> Instrument:
    """Return the profile for ``symbol`` or raise a clear error."""
    try:
        return INSTRUMENTS[symbol.upper()]
    except KeyError as exc:
        choices = ", ".join(sorted(INSTRUMENTS))
        raise KeyError(f"unknown instrument {symbol!r}; choose one of {choices}") from exc


def bar_minutes(timeframe: str) -> int:
    if timeframe.endswith("m"):
        return int(timeframe[:-1])
    raise ValueError(f"unsupported timeframe {timeframe!r}; use 1m, 5m or 15m")
