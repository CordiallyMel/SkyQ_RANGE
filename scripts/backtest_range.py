"""Backtest the SkyQ RANGE channel scalper on the supplied 1-minute data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from config.settings import (
    ATR_FILTER,
    ATR_MIN,
    BREAKOUT_TICKS,
    COOLDOWN_BARS,
    DEFAULT_SYMBOL,
    ENTRY_ZONE,
    FLAT_AT_MINUTE,
    INSTRUMENTS,
    MAX_HOLD_BARS,
    MIN_CHANNEL_PCT,
    MIN_CHANNEL_WIDTH,
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
from src.backtest import BacktestResult, run_backtest
from src.indicators import load_bars, load_event_calendar, load_raw_1m


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SkyQ RANGE channel-scalper backtest")
    parser.add_argument("--symbol", choices=sorted(INSTRUMENTS), default=DEFAULT_SYMBOL)
    parser.add_argument("--timeframe", choices=["1m", "5m", "15m"], default="1m")
    parser.add_argument("--start", default=None, help="inclusive start, e.g. 2025-09-07 18:00")
    parser.add_argument("--end", default=None, help="inclusive end, e.g. 2025-09-26 16:59")
    parser.add_argument("--sides", choices=["long", "short", "both"], default=SIDES)
    parser.add_argument(
        "--quantity",
        type=int,
        default=None,
        help=f"contracts/shares; default {QUANTITY} shares for stocks and 1 for futures",
    )
    parser.add_argument("--entry-zone", type=float, default=None)
    parser.add_argument("--min-channel-width", type=float, default=MIN_CHANNEL_WIDTH)
    parser.add_argument("--min-width-pct", type=float, default=None)
    parser.add_argument("--max-channel-width", type=float, default=None)
    parser.add_argument("--stop-ticks", type=float, default=None)
    parser.add_argument("--tp-mode", choices=["channel", "fixed"], default=TP_MODE)
    parser.add_argument("--tp-pct", type=float, default=TP_CHANNEL_PCT)
    parser.add_argument("--tp-fixed-ticks", type=float, default=TP_FIXED_TICKS)
    parser.add_argument("--cooldown", type=int, default=COOLDOWN_BARS)
    parser.add_argument("--no-overlap", action="store_true", help="reject overlapping channels")
    parser.add_argument("--breakout-ticks", type=float, default=BREAKOUT_TICKS)
    parser.add_argument("--atr-filter", dest="atr_filter", action="store_true", default=ATR_FILTER)
    parser.add_argument("--no-atr-filter", dest="atr_filter", action="store_false")
    parser.add_argument("--atr-min", type=float, default=ATR_MIN)
    parser.add_argument("--atr-percentile", type=float, default=None)
    parser.add_argument("--session", choices=list(SESSION_CHOICES), default=SESSION)
    parser.add_argument(
        "--skip-hours",
        default="",
        help="comma-separated decision-bar hours to skip; empty disables",
    )
    parser.add_argument(
        "--skip-events",
        default="",
        help="comma-separated macro event codes to skip; empty disables",
    )
    parser.add_argument("--no-creation-entry", action="store_true")
    parser.add_argument("--require-inside", action="store_true")
    parser.add_argument("--ambiguous", choices=["long", "short", "skip"], default="long")
    parser.add_argument(
        "--entry-mode",
        choices=["next_open", "zone", "close"],
        default="next_open",
        help="next_open is the causal live rule; zone reproduces Pine's reference fill",
    )
    parser.add_argument("--entry-slip", type=float, default=SLIPPAGE_TICKS)
    parser.add_argument("--exit-slip", type=float, default=SLIPPAGE_TICKS)
    parser.add_argument("--commission", type=float, default=None, help="per contract per side override")
    parser.add_argument("--max-hold-bars", type=int, default=None)
    parser.add_argument("--flat-at", type=int, default=FLAT_AT_MINUTE, help="minute-of-day ET, e.g. 955")
    parser.add_argument("--target-first", action="store_true")
    parser.add_argument("--data", default=None, help="override the 1-minute parquet")
    parser.add_argument("--no-intrabar", action="store_true")
    parser.add_argument("--csv", default=None)
    parser.add_argument("--monthly", action="store_true")
    parser.add_argument("--trades", type=int, default=5)
    args = parser.parse_args()
    try:
        book = book_preset(args.symbol)
    except KeyError:
        book = None
    if args.entry_zone is None:
        args.entry_zone = book.entry_zone if book else ENTRY_ZONE
    if args.stop_ticks is None:
        args.stop_ticks = book.stop_ticks if book else STOP_TICKS
    if args.min_width_pct is None:
        args.min_width_pct = book.min_channel_pct if book else MIN_CHANNEL_PCT
    if args.max_hold_bars is None:
        args.max_hold_bars = book.max_hold_bars if book else MAX_HOLD_BARS
    return args


def _fmt(value: float, dollars: bool = False) -> str:
    if dollars:
        return f"{value:+,.2f}"
    return f"{value:,.2f}"


def _hours(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(",") if part.strip())


def _codes(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _default_data(symbol: str) -> Path:
    profile = instrument(symbol)
    for candidate in (
        Path(profile.data_file_1m()),
        Path(f"data/{profile.data_symbol}_1m.parquet"),
        Path(f"data/{profile.data_symbol}_1m_indicators.parquet"),
    ):
        if candidate.exists():
            return candidate
    return Path(profile.data_file_1m())


def _print_result(result: BacktestResult, show_trades: int = 5) -> None:
    s = result.summary()
    print()
    print(f"=== SkyQ RANGE {result.profile.symbol} {result.profile.description} ===")
    if result.start is not None and result.end is not None:
        print(f"bars={result.bars:,}  {result.start:%Y-%m-%d %H:%M} -> {result.end:%Y-%m-%d %H:%M}")
    print(
        f"trades={int(s['trades']):,}  wins={int(s['wins']):,}  losses={int(s['losses']):,}  "
        f"win_rate={s['win_rate'] * 100:.2f}%  avg_bars={s['avg_bars']:.1f}  "
        f"exposure={s['exposure'] * 100:.1f}%"
    )
    print(
        f"gross={_fmt(s['gross'], True)}  costs={_fmt(-s['costs'], True)}  "
        f"net={_fmt(s['net'], True)}  expectancy={_fmt(s['expectancy'], True)}"
    )
    print(
        f"avg_win={_fmt(s['avg_win'], True)}  avg_loss={_fmt(s['avg_loss'], True)}  "
        f"rr={s['rr']:.3f}  pf={s['pf']:.3f}  max_dd={_fmt(-s['max_drawdown'], True)}"
    )
    print(
        f"net/month={_fmt(s['net_per_month'], True)}  trades/month={s['trades_per_month']:.1f}  "
        f"sharpe={s['sharpe']:.2f}  sortino={s['sortino']:.2f}  "
        f"positive_years={int(s['positive_years'])}/{int(s['years'])}  "
        f"positive_months={int(s['positive_months'])}/{int(s['months'])}"
    )
    print(
        f"channels={int(s['channels']):,}  avg_channel_width={s['avg_channel_width']:.2f} pts  "
        f"avg_risk=${s['avg_risk']:,.2f}  trades/day={s['trades_per_day']:.2f}"
    )
    if result.gate_counts:
        gates = "  ".join(f"{key}={value:,}" for key, value in sorted(result.gate_counts.items()))
        print(f"gates: {gates}")

    side = result.side_frame()
    if not side.empty:
        print()
        print("BY SIDE")
        print(
            side.to_string(
                index=False,
                formatters={
                    "win_rate": lambda value: f"{value * 100:.2f}%",
                    "net": lambda value: f"{value:+,.2f}",
                    "avg_win": lambda value: f"{value:+,.2f}",
                    "avg_loss": lambda value: f"{value:+,.2f}",
                    "pf": lambda value: f"{value:.2f}",
                },
            )
        )

    yearly = result.yearly_frame()
    if not yearly.empty:
        print()
        print("BY YEAR")
        print(
            yearly.to_string(
                index=False,
                formatters={
                    "win_rate": lambda value: f"{value * 100:.2f}%",
                    "net": lambda value: f"{value:+,.2f}",
                },
            )
        )

    if result.trades and show_trades > 0:
        print()
        print(f"LAST {min(show_trades, len(result.trades))} TRADES")
        frame = result.trades_frame().tail(show_trades)
        print(
            frame[
                [
                    "side", "bar_time", "entry_time", "entry_price", "stop_price",
                    "target_price", "exit_time", "exit_price", "exit_reason",
                    "net_pnl", "bars_held", "channel_width",
                ]
            ].to_string(index=False)
        )


def run(args: argparse.Namespace) -> int:
    profile = instrument(args.symbol)
    if args.quantity is None:
        args.quantity = 1 if profile.is_future else QUANTITY
    path = Path(args.data) if args.data else _default_data(args.symbol)
    if not path.exists():
        print(f"missing data file: {path}", file=sys.stderr)
        return 2

    frame = load_bars(
        str(path),
        timeframe=args.timeframe,
        start=args.start,
        end=args.end,
    )
    intrabar = None
    if not args.no_intrabar and args.timeframe != "1m":
        intrabar = load_raw_1m(str(path), start=args.start, end=args.end)
    elif args.timeframe == "1m":
        intrabar = None

    skip_hours = _hours(args.skip_hours)
    event_codes = _codes(args.skip_events)
    calendar = load_event_calendar() if event_codes else None
    print(
        f"SkyQ RANGE: {profile.symbol} {args.timeframe}, "
        f"preset={args.stop_ticks:g}t/{args.min_width_pct:.2%}, sides={args.sides}, qty={args.quantity}, "
        f"zone={args.entry_zone:.2f}, stop={args.stop_ticks:g} ticks, tp={args.tp_mode} "
        f"{args.tp_pct if args.tp_mode == 'channel' else args.tp_fixed_ticks:g}, "
        f"cooldown={args.cooldown}, overlap={not args.no_overlap}, "
        f"min_width={args.min_channel_width:g}, session={args.session}, atr_filter={args.atr_filter}, "
        f"entry={args.entry_mode}, intrabar={'1m' if intrabar is not None else 'signal'}"
    )
    result = run_backtest(
        frame,
        profile,
        start=args.start,
        end=args.end,
        sides=args.sides,
        quantity=args.quantity,
        entry_zone=args.entry_zone,
        min_channel_width=args.min_channel_width,
        min_channel_pct=args.min_width_pct,
        max_channel_width=args.max_channel_width,
        stop_ticks=args.stop_ticks,
        tp_mode=args.tp_mode,
        tp_channel_pct=args.tp_pct,
        tp_fixed_ticks=args.tp_fixed_ticks,
        cooldown_bars=args.cooldown,
        overlap=not args.no_overlap,
        breakout_ticks=args.breakout_ticks,
        atr_filter=args.atr_filter,
        atr_min=args.atr_min,
        atr_percentile=args.atr_percentile,
        session=args.session,
        skip_hours=skip_hours,
        event_codes=event_codes,
        calendar=calendar,
        allow_creation_entry=not args.no_creation_entry,
        require_inside=args.require_inside,
        ambiguous=args.ambiguous,
        entry_mode=args.entry_mode,
        entry_slippage_ticks=args.entry_slip,
        exit_slippage_ticks=args.exit_slip,
        commission_per_contract=args.commission,
        max_hold_bars=args.max_hold_bars,
        flat_at_minute=None if args.flat_at is not None and args.flat_at < 0 else args.flat_at,
        timeframe=args.timeframe,
        intrabar=intrabar,
        assume_stop_first=not args.target_first,
    )
    if result.bars == 0:
        print("no bars in the requested range", file=sys.stderr)
        return 1
    _print_result(result, show_trades=args.trades)

    if args.monthly:
        monthly = result.monthly_frame()
        if not monthly.empty:
            print()
            print("BY MONTH")
            print(
                monthly.to_string(
                    index=False,
                    formatters={
                        "win_rate": lambda value: f"{value * 100:.2f}%",
                        "net": lambda value: f"{value:+,.2f}",
                    },
                )
            )

    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        result.trades_frame().to_csv(csv_path, index=False)
        print(f"\nwrote {len(result.trades):,} trades to {csv_path}")
    return 0


def main() -> int:
    return run(_parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
