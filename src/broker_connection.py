"""IBKR connectivity and intraday history for SkyQ RANGE.

The live default is a stock (SPY), but the same connection layer still resolves
front-month futures for the research variants.
"""

from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from datetime import date as date_
from datetime import datetime, timedelta
from typing import Iterator, Sequence

import pandas as pd
from ib_async import IB, BarData, Contract, Future, Stock

from config.settings import CONNECTION, ConnectionConfig, Instrument, NY_TZ

log = logging.getLogger(__name__)

BAR_COLUMNS = ("open", "high", "low", "close", "volume")


class ContractSpecError(RuntimeError):
    """Raised when a live contract disagrees with its configured specification."""


def connect(config: ConnectionConfig = CONNECTION) -> IB:
    """Connect to IB Gateway / TWS and return a ready :class:`IB` instance."""
    ib = IB()
    ib.connect(
        config.host,
        config.port,
        clientId=config.client_id,
        timeout=config.timeout,
        readonly=config.readonly,
        account=config.account or "",
    )
    log.info(
        "Connected to IBKR at %s:%s (clientId=%s, readonly=%s)",
        config.host,
        config.port,
        config.client_id,
        config.readonly,
    )
    return ib


@contextmanager
def ib_session(config: ConnectionConfig = CONNECTION) -> Iterator[IB]:
    """Connect on entry and disconnect on exit."""
    ib = connect(config)
    try:
        yield ib
    finally:
        if ib.isConnected():
            ib.disconnect()
            log.info("Disconnected from IBKR")


def _as_future(contract: Contract) -> Future:
    if isinstance(contract, Future):
        return contract
    return Future(
        conId=contract.conId,
        symbol=contract.symbol,
        lastTradeDateOrContractMonth=contract.lastTradeDateOrContractMonth,
        exchange=contract.exchange,
        primaryExchange=contract.primaryExchange,
        currency=contract.currency,
        localSymbol=contract.localSymbol,
        tradingClass=contract.tradingClass,
        multiplier=contract.multiplier,
    )


def _parse_expiry(value: str) -> date_ | None:
    digits = "".join(character for character in (value or "") if character.isdigit())
    if len(digits) < 6:
        return None
    year, month = int(digits[:4]), int(digits[4:6])
    if year < 1900 or not 1 <= month <= 12:
        return None
    if len(digits) >= 8:
        try:
            return date_(year, month, int(digits[6:8]))
        except ValueError:
            return None
    next_month = date_(year + 1, 1, 1) if month == 12 else date_(year, month + 1, 1)
    return next_month - timedelta(days=1)


def _qualify_future(ib: IB, contract: Contract) -> Future | None:
    future = _as_future(contract)
    qualified = ib.qualifyContracts(future)
    if not qualified or qualified[0] is None:
        return None
    if getattr(qualified[0], "secType", None) != "FUT":
        return None
    return _as_future(qualified[0])


def _next_quarterly_expiries(today: date_, count: int = 4) -> list[str]:
    quarter_months = (3, 6, 9, 12)
    result: list[str] = []
    year = today.year
    while len(result) < count:
        for month in quarter_months:
            if (year, month) >= (today.year, today.month):
                result.append(f"{year}{month:02d}")
                if len(result) >= count:
                    break
        year += 1
    return result


def future_contract(profile: Instrument, expiry: str) -> Future:
    return Future(
        symbol=profile.symbol,
        lastTradeDateOrContractMonth=expiry,
        exchange=profile.exchange,
        currency="USD",
        multiplier=f"{profile.multiplier:g}",
    )


def front_future(ib: IB, profile: Instrument) -> Future:
    """Resolve the nearest non-expired future for ``profile``."""
    today = datetime.now(tz=NY_TZ).date()
    details = ib.reqContractDetails(
        Future(symbol=profile.symbol, exchange=profile.exchange, currency="USD")
    )

    candidates: list[tuple[date_, int, Contract]] = []
    for contract_details in details or []:
        contract = contract_details.contract
        if contract is None or getattr(contract, "secType", None) != "FUT":
            continue
        if contract.symbol and contract.symbol.upper() != profile.symbol.upper():
            continue
        if contract.currency and contract.currency.upper() != "USD":
            continue
        expiry = _parse_expiry(
            contract_details.realExpirationDate
        ) or _parse_expiry(contract.lastTradeDateOrContractMonth)
        # Do not open a new position in a contract that expires today; its
        # liquidity and settlement behaviour are different from the next month.
        if expiry is None or expiry <= today:
            continue
        candidates.append((expiry, contract.conId, contract))

    candidates.sort(key=lambda item: (item[0], item[1]))
    last_error: Exception | None = None
    for expiry, _, contract in candidates:
        qualified = _qualify_future(ib, contract)
        if qualified is not None:
            return qualified
        last_error = ContractSpecError(
            f"Failed to qualify {profile.symbol} {expiry:%Y-%m} contract"
        )
        log.warning("%s; trying next expiry", last_error)

    if not details:
        log.warning("IBKR returned no %s futures; using quarterly fallback", profile.symbol)
    elif not candidates:
        log.warning("No active %s futures; using quarterly fallback", profile.symbol)

    for expiry in _next_quarterly_expiries(today):
        qualified = _qualify_future(ib, future_contract(profile, expiry))
        if qualified is not None:
            log.info("Resolved %s front month via quarterly fallback: %s", profile.symbol, expiry)
            return qualified

    raise ContractSpecError(
        f"Could not resolve an active {profile.symbol} futures contract "
        f"(enumerated={len(candidates)}, today={today.isoformat()})"
    ) from last_error


def stock_contract(profile: Instrument) -> Stock:
    """Build the SMART-routed US stock contract for a stock profile."""
    return Stock(symbol=profile.symbol, exchange=profile.exchange or "SMART", currency="USD")


def resolve_contract(ib: IB, profile: Instrument) -> Contract:
    """Resolve and qualify the live contract for a stock or future profile."""
    if profile.is_future:
        return front_future(ib, profile)
    contract = stock_contract(profile)
    qualified = ib.qualifyContracts(contract)
    if not qualified or qualified[0] is None:
        raise ContractSpecError(f"Could not qualify stock {profile.symbol}")
    live = qualified[0]
    if getattr(live, "secType", None) != "STK":
        raise ContractSpecError(
            f"{profile.symbol} resolved to {getattr(live, 'secType', None)!r}, expected STK"
        )
    return live


def verify_contract_spec(ib: IB, contract: Contract, profile: Instrument) -> None:
    """Verify the live contract against the profile for stocks or futures."""
    if profile.is_future:
        verify_future_spec(ib, contract, profile)
        return
    details = ib.reqContractDetails(contract)
    if not details:
        raise LookupError(f"IBKR returned no contract details for {contract}")
    live_tick = details[0].minTick
    if live_tick and not math.isclose(live_tick, profile.tick_size, abs_tol=1e-9):
        raise ContractSpecError(
            f"{profile.symbol} stock minTick={live_tick} (expected {profile.tick_size})"
        )
    log.info(
        "Verified %s stock: secType=%s minTick=%s",
        profile.symbol,
        contract.secType,
        live_tick,
    )


def verify_future_spec(ib: IB, contract: Contract, profile: Instrument) -> None:
    """Assert the live multiplier and minimum tick match the profile."""
    details = ib.reqContractDetails(contract)
    if not details:
        raise LookupError(f"IBKR returned no contract details for {contract}")
    contract_details = details[0]
    live_multiplier = contract_details.contract.multiplier if contract_details.contract else ""
    problems: list[str] = []
    try:
        if live_multiplier and not math.isclose(
            float(live_multiplier), float(profile.multiplier), rel_tol=1e-9
        ):
            problems.append(
                f"multiplier={live_multiplier!r} (expected {profile.multiplier!r})"
            )
    except ValueError:
        problems.append(f"unparseable multiplier={live_multiplier!r}")

    if not math.isclose(contract_details.minTick, profile.tick_size, abs_tol=1e-9):
        problems.append(
            f"minTick={contract_details.minTick} (expected {profile.tick_size})"
        )

    if problems:
        raise ContractSpecError(
            f"{profile.symbol} contract spec mismatch: " + "; ".join(problems)
        )

    log.info(
        "Verified %s %s: multiplier=%s minTick=%s",
        profile.symbol,
        contract_details.contract.localSymbol if contract_details.contract else "",
        live_multiplier,
        contract_details.minTick,
    )


def bars_to_frame(bars: Sequence[BarData]) -> pd.DataFrame:
    """Convert IBKR intraday bars to a New York-indexed OHLCV DataFrame."""
    if not bars:
        return pd.DataFrame(columns=list(BAR_COLUMNS))
    records: list[dict[str, object]] = []
    for bar in bars:
        value = pd.Timestamp(bar.date)
        if value.tzinfo is None:
            value = value.tz_localize(NY_TZ)
        else:
            value = value.tz_convert(NY_TZ)
        records.append(
            {
                "time": value,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
        )
    frame = pd.DataFrame(records)
    return (
        frame.drop_duplicates(subset="time", keep="last")
        .set_index("time")
        .sort_index()
    )


def fetch_intraday_bars(
    ib: IB,
    contract: Contract,
    *,
    duration: str = "10 D",
    bar_size: str = "1 min",
    end: datetime | None = None,
    use_rth: bool | None = None,
) -> pd.DataFrame:
    """Fetch intraday bars.

    Stocks default to regular trading hours; futures default to the full
    Globex session.  The SkyQ RANGE runner passes ``use_rth=False`` so the 100-bar
    normalization sees the same extended-hours history as the backtest; entries
    remain restricted to the configured session.
    """
    if use_rth is None:
        use_rth = getattr(contract, "secType", None) != "FUT"
    bars = ib.reqHistoricalData(
        contract,
        endDateTime=end if end is not None else "",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=bool(use_rth),
        formatDate=2,
        keepUpToDate=False,
    )
    return bars_to_frame(bars)
