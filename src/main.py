"""Live / paper runner for the SkyQ RANGE channel scalper.

The live book is SPY/QQQ only.  SPY is the full-history reference: both sides,
regular hours, 1-minute bars, a 20% channel-edge entry zone, a 100-tick stop
beyond the channel, a 20%-of-channel target, a 0.20% minimum channel width, a
120-bar time stop and a 15:55 ET flat.  QQQ uses its own 150-tick / 0.40% /
240-bar preset.  Futures are research-only and are not viable after costs.

Monitor-only is the default.  ``--execute`` is required to place orders, and the
default port is the paper Gateway (4002).  The runner fetches all available
1-minute bars (including extended hours) so the causal 100-bar normalization
matches the backtest, but it only submits entries during the configured
session.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time as time_module
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from ib_async import IB

from config.settings import (
    ATR_FILTER,
    ATR_MIN,
    BREAKOUT_TICKS,
    CONNECTION,
    COOLDOWN_BARS,
    DEFAULT_SYMBOL,
    ENTRY_ZONE,
    FLAT_AT_MINUTE,
    INSTRUMENTS,
    LIVE_SYMBOLS,
    MAX_HOLD_BARS,
    MIN_CHANNEL_PCT,
    MIN_CHANNEL_WIDTH,
    MIN_HISTORY_BARS,
    NY_TZ,
    OVERLAP,
    QUANTITY,
    SESSION,
    SESSION_CHOICES,
    SIDES,
    SLIPPAGE_TICKS,
    STOP_TICKS,
    TP_CHANNEL_PCT,
    TP_FIXED_TICKS,
    TP_MODE,
    book_preset,
    instrument,
)
from src.broker_connection import (
    connect,
    fetch_intraday_bars,
    resolve_contract,
    verify_contract_spec,
)
from src.indicators import (
    add_features,
    completed,
    gate_ready,
    session_allows,
)
from src.order_manager import LivePosition, position_from_plan
from src.strategy_logic import (
    EntryPlan,
    Evaluation,
    Signal,
    evaluate_last,
    plan_entry,
    update_channels,
)

log = logging.getLogger("skyq.range")

_LIVE_PORTS = frozenset({4001, 7496})


def _configure_logging(log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SkyQ RANGE channel scalper")
    parser.add_argument("--symbol", choices=sorted(INSTRUMENTS), default=DEFAULT_SYMBOL)
    parser.add_argument("--timeframe", choices=["1m", "5m", "15m"], default="1m")
    parser.add_argument(
        "--quantity",
        type=int,
        default=None,
        help=f"contracts/shares; default {QUANTITY} shares for stocks and 1 for futures",
    )
    parser.add_argument("--sides", choices=["long", "short", "both"], default=SIDES)
    parser.add_argument("--session", choices=list(SESSION_CHOICES), default=SESSION)
    parser.add_argument("--entry-zone", type=float, default=None)
    parser.add_argument("--min-channel-width", type=float, default=MIN_CHANNEL_WIDTH)
    parser.add_argument("--min-width-pct", type=float, default=None)
    parser.add_argument("--stop-ticks", type=float, default=None)
    parser.add_argument("--tp-mode", choices=["channel", "fixed"], default=TP_MODE)
    parser.add_argument("--tp-pct", type=float, default=TP_CHANNEL_PCT)
    parser.add_argument("--tp-fixed-ticks", type=float, default=TP_FIXED_TICKS)
    parser.add_argument("--cooldown", type=int, default=COOLDOWN_BARS)
    parser.add_argument("--no-overlap", action="store_true")
    parser.add_argument("--atr-filter", dest="atr_filter", action="store_true", default=ATR_FILTER)
    parser.add_argument("--no-atr-filter", dest="atr_filter", action="store_false")
    parser.add_argument("--atr-min", type=float, default=ATR_MIN)
    parser.add_argument("--max-hold-bars", type=int, default=None)
    parser.add_argument("--flat-at", type=int, default=FLAT_AT_MINUTE, help="minute-of-day ET")
    parser.add_argument("--max-bar-age-minutes", type=float, default=5.0)
    parser.add_argument("--history-duration", default="30 D")
    parser.add_argument("--poll-seconds", type=float, default=15.0)
    parser.add_argument("--execute", action="store_true", help="submit bracket orders")
    parser.add_argument("--check", action="store_true", help="print the latest decision and exit")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--client-id", type=int, default=None)
    parser.add_argument("--allow-delayed-data", action="store_true")
    parser.add_argument(
        "--manage-existing",
        action="store_true",
        help="start even when a broker position exists; new entries stay blocked until flat",
    )
    parser.add_argument("--flatten-on-exit", action="store_true")
    parser.add_argument("--log-file", default=None)
    args = parser.parse_args()
    try:
        book = book_preset(args.symbol)
    except KeyError:
        book = None
    if args.quantity is None:
        args.quantity = book.quantity if book else QUANTITY
    if args.entry_zone is None:
        args.entry_zone = book.entry_zone if book else ENTRY_ZONE
    if args.stop_ticks is None:
        args.stop_ticks = book.stop_ticks if book else STOP_TICKS
    if args.min_width_pct is None:
        args.min_width_pct = book.min_channel_pct if book else MIN_CHANNEL_PCT
    if args.max_hold_bars is None:
        args.max_hold_bars = book.max_hold_bars if book else MAX_HOLD_BARS
    return args


def _bar_minutes(timeframe: str) -> int:
    return {"1m": 1, "5m": 5, "15m": 15}[timeframe]


def _bar_size(timeframe: str) -> str:
    return {"1m": "1 min", "5m": "5 mins", "15m": "15 mins"}[timeframe]


def _last_price(ib: IB, contract, delayed: bool) -> float | None:
    ticker = None
    try:
        if delayed:
            ib.reqMarketDataType(3)
        ticker = ib.reqMktData(contract, "", False, False)
        for _ in range(12):
            ib.sleep(0.25)
            for candidate in (ticker.marketPrice(), ticker.last, ticker.close):
                if candidate is None:
                    continue
                value = float(candidate)
                if math.isfinite(value) and value > 0:
                    return value
        return None
    except Exception:
        log.exception("Could not read a price")
        return None
    finally:
        if ticker is not None:
            try:
                ib.cancelMktData(contract)
            except Exception:
                pass


def _existing_position(ib: IB, profile) -> float:
    try:
        ib.reqPositions()
        ib.sleep(0.5)
        for position in ib.positions():
            held = position.contract
            if getattr(held, "secType", None) != ("FUT" if profile.is_future else "STK"):
                continue
            if held.symbol and held.symbol.upper() != profile.symbol.upper():
                continue
            if abs(position.position) > 0:
                return float(position.position)
    except Exception:
        log.exception("Could not read broker positions")
    return 0.0


def _bar_age_minutes(frame: pd.DataFrame, now: datetime, minutes: int) -> float:
    if frame.empty:
        return float("inf")
    latest = pd.Timestamp(frame.index[-1])
    close = latest + pd.Timedelta(minutes=minutes)
    return (pd.Timestamp(now) - close).total_seconds() / 60.0


def _plan_valid(plan: EntryPlan, price: float) -> bool:
    if plan.side.value == "LONG":
        return plan.stop_price < price < plan.target_price
    return plan.target_price < price < plan.stop_price


def _format_plan(plan: EntryPlan) -> str:
    return (
        f"{plan.side.value} ref={plan.entry_price:.2f} stop={plan.stop_price:.2f} "
        f"target={plan.target_price:.2f} risk={plan.stop_distance:.2f} pts "
        f"reward={plan.target_distance:.2f} pts rr={plan.reward_risk:.2f} "
        f"qty={plan.quantity}"
    )


def _replay_channels(
    frame: pd.DataFrame,
    profile,
    *,
    overlap: bool,
    breakout_ticks: float,
):
    """Replay the channel lifecycle over the fetched window."""
    channels = []
    for index in range(len(frame)):
        row = frame.iloc[index]
        channels, _ = update_channels(
            channels,
            index=index,
            close=float(row["close"]),
            channel_event=bool(row["channel_event"]),
            top=float(row["channel_top"]),
            bottom=float(row["channel_bottom"]),
            bar_time=pd.Timestamp(frame.index[index]).to_pydatetime(),
            profile=profile,
            overlap=overlap,
            breakout_ticks=breakout_ticks,
        )
    return channels


def _next_open(now: datetime) -> datetime:
    """Next 09:30 ET, used to avoid a busy poll loop outside RTH."""
    target = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def _wait_for_bar(minutes: int) -> None:
    now = datetime.now(tz=NY_TZ)
    floored = now.replace(second=0, microsecond=0)
    remainder = floored.minute % minutes
    target = floored + timedelta(minutes=(minutes - remainder) if remainder else minutes)
    target += timedelta(seconds=3)
    delay = (target - now).total_seconds()
    if delay > 0:
        time_module.sleep(delay)


def _decision_text(evaluation: Evaluation) -> str:
    if evaluation.signal is not None:
        return "SIGNAL " + evaluation.signal.describe()
    return f"no trade ({evaluation.reason or 'none'})"


def run(args: argparse.Namespace) -> int:
    profile = instrument(args.symbol)
    if profile.symbol not in LIVE_SYMBOLS:
        log.error(
            "%s is research-only. The RANGE live book is SPY/QQQ; see "
            "reports/futures_gate.csv for the negative futures evidence.",
            profile.symbol,
        )
        return 2
    if args.quantity is None:
        args.quantity = book_preset(profile.symbol).quantity
    config = CONNECTION
    if args.host or args.port or args.client_id:
        from dataclasses import replace

        config = replace(
            config,
            host=args.host or config.host,
            port=args.port if args.port is not None else config.port,
            client_id=args.client_id if args.client_id is not None else config.client_id,
        )
    if config.port in _LIVE_PORTS and not args.execute:
        log.warning(
            "Connected to a live port in monitor mode; pass --execute to submit orders"
        )

    ib = connect(config)
    live: LivePosition | None = None
    last_signal_at: datetime | None = None
    minutes = _bar_minutes(args.timeframe)

    try:
        contract = resolve_contract(ib, profile)
        verify_contract_spec(ib, contract, profile)
        log.info(
            "SkyQ RANGE %s %s qty=%d session=%s sides=%s zone=%.2f stop=%.0f ticks "
            "tp=%s %.2f min_width=%.2f min_width_pct=%s execute=%s",
            profile.symbol,
            args.timeframe,
            args.quantity,
            args.session,
            args.sides,
            args.entry_zone,
            args.stop_ticks,
            args.tp_mode,
            args.tp_pct if args.tp_mode == "channel" else args.tp_fixed_ticks,
            args.min_channel_width,
            args.min_width_pct,
            args.execute,
        )

        while True:
            now = datetime.now(tz=NY_TZ)
            minute_now = now.hour * 60 + now.minute
            in_session = session_allows(now, args.session)
            if not in_session:
                if live is not None:
                    snapshot = live.snapshot()
                    if snapshot.position_open:
                        log.info("Outside session with an open position; flattening")
                        live.flatten("SESSION_END")
                        live.wait_flat(10.0)
                    live = None
                if args.check:
                    print("outside session")
                    return 0
                delay = min(60.0, max(10.0, (_next_open(now) - now).total_seconds()))
                time_module.sleep(delay)
                continue

            bars = fetch_intraday_bars(
                ib,
                contract,
                duration=args.history_duration,
                bar_size=_bar_size(args.timeframe),
                use_rth=False,
            )
            bars = completed(bars, now, minutes)
            if len(bars) < MIN_HISTORY_BARS:
                log.warning("Only %d completed bars; need %d", len(bars), MIN_HISTORY_BARS)
                if args.check:
                    print(f"not enough history: {len(bars)} bars")
                    return 1
                time_module.sleep(max(5.0, args.poll_seconds))
                continue

            frame = gate_ready(add_features(bars, timeframe=args.timeframe))
            if frame.empty:
                log.warning("No feature-ready bars")
                time_module.sleep(max(5.0, args.poll_seconds))
                continue
            age = _bar_age_minutes(frame, now, minutes)
            if age > args.max_bar_age_minutes:
                log.warning("Latest completed bar is %.1f minutes old; skipping", age)
                if args.check:
                    print(f"stale data: {age:.1f} minutes")
                    return 1
                time_module.sleep(max(5.0, args.poll_seconds))
                continue

            channels = _replay_channels(
                frame,
                profile,
                overlap=not args.no_overlap,
                breakout_ticks=BREAKOUT_TICKS,
            )
            evaluation = evaluate_last(
                frame,
                channels,
                profile,
                sides=args.sides,
                entry_zone=args.entry_zone,
                min_channel_pct=args.min_width_pct,
                atr_filter=args.atr_filter,
                atr_min=args.atr_min,
                cooldown_bars=0,
                session=args.session,
                allow_creation_entry=True,
                require_inside=False,
                ambiguous="long",
            )

            # A live entry is never submitted in the last minutes before flat.
            if args.flat_at is not None and minute_now >= args.flat_at:
                evaluation = Evaluation(reason="flat_window")

            # Time-based cooldown, because the fetched frame shifts each poll.
            if evaluation.signal is not None and last_signal_at is not None:
                signal_bar = pd.Timestamp(evaluation.signal.bar_time)
                if signal_bar - last_signal_at < timedelta(minutes=args.cooldown * minutes):
                    evaluation = Evaluation(reason="cooldown")

            # A channel narrower than the absolute or price-relative floor is
            # not a setup.
            if evaluation.signal is not None:
                width = evaluation.signal.channel.width
                latest_close = float(frame["close"].iloc[-1])
                relative_floor = (
                    latest_close * args.min_width_pct
                    if args.min_width_pct is not None
                    else 0.0
                )
                if width < max(args.min_channel_width, relative_floor):
                    evaluation = Evaluation(reason="narrow_channel")

            log.info(
                "latest=%s close=%.2f channels=%d %s",
                frame.index[-1],
                float(frame["close"].iloc[-1]),
                len(channels),
                _decision_text(evaluation),
            )

            if live is not None:
                snapshot = live.snapshot()
                if snapshot.flat:
                    log.info(
                        "Closed %s: %s pnl=%.2f entry=%.2f exit=%s",
                        profile.symbol,
                        snapshot.exit_reason,
                        snapshot.pnl,
                        snapshot.entry_price,
                        snapshot.exit_price,
                    )
                    live = None
                else:
                    held_minutes = (now - live.entry_time).total_seconds() / 60.0
                    if snapshot.orphaned:
                        log.error("Bracket is orphaned; flattening")
                        live.flatten("ORPHANED")
                    elif args.max_hold_bars > 0 and held_minutes >= args.max_hold_bars * minutes:
                        log.info("Time stop after %.0f minutes", held_minutes)
                        live.flatten("MAX_HOLD")
                    elif args.flat_at is not None and minute_now >= args.flat_at:
                        log.info("Session flat at %02d:%02d", minute_now // 60, minute_now % 60)
                        live.flatten("FLAT")

            if live is None and evaluation.signal is not None:
                if _existing_position(ib, profile) != 0 and not args.manage_existing:
                    log.warning("Broker already holds %s; not entering", profile.symbol)
                else:
                    price = _last_price(ib, contract, args.allow_delayed_data)
                    if price is None:
                        log.warning("No market price; signal skipped")
                    else:
                        try:
                            plan = plan_entry(
                                evaluation.signal,
                                price,
                                profile,
                                quantity=args.quantity,
                                stop_ticks=args.stop_ticks,
                                tp_mode=args.tp_mode,
                                tp_channel_pct=args.tp_pct,
                                tp_fixed_ticks=args.tp_fixed_ticks,
                            )
                        except ValueError as exc:
                            log.warning("Invalid bracket: %s", exc)
                            plan = None
                        if plan is not None and not _plan_valid(plan, price):
                            log.warning("Price already outside the bracket; signal skipped")
                            plan = None
                        if plan is not None:
                            log.info("PLAN %s", _format_plan(plan))
                            last_signal_at = pd.Timestamp(evaluation.signal.bar_time)
                            if args.execute:
                                live = position_from_plan(ib, contract, profile, plan)
                                log.info("Bracket submitted")

            if args.check:
                if evaluation.signal is not None:
                    print(evaluation.signal.describe())
                    try:
                        price = _last_price(ib, contract, args.allow_delayed_data)
                        if price is not None:
                            plan = plan_entry(
                                evaluation.signal,
                                price,
                                profile,
                                quantity=args.quantity,
                                stop_ticks=args.stop_ticks,
                                tp_mode=args.tp_mode,
                                tp_channel_pct=args.tp_pct,
                                tp_fixed_ticks=args.tp_fixed_ticks,
                            )
                            print(_format_plan(plan))
                    except ValueError as exc:
                        print(f"bracket invalid: {exc}")
                else:
                    print(_decision_text(evaluation))
                return 0

            _wait_for_bar(minutes)

    except KeyboardInterrupt:
        log.info("Interrupted")
        if live is not None and args.flatten_on_exit:
            live.flatten("SHUTDOWN")
            live.wait_flat(10.0)
        return 0
    except Exception:
        log.exception("Runner failed")
        if live is not None and args.flatten_on_exit:
            live.flatten("ERROR")
            live.wait_flat(10.0)
        return 1
    finally:
        if ib.isConnected():
            ib.disconnect()
            log.info("Disconnected from IBKR")


def main() -> int:
    args = _parse_args()
    _configure_logging(args.log_file)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
