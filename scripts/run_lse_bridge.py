"""Run the one-minute LSE bridge.

This is the only process that needs the LSE key and the only process that holds
the WebSocket.  It downloads a one-minute warmup through yesterday, builds live
one-minute bars, appends them to the durable cache, and publishes an atomic
heartbeat under ``data/lse/shared/<SYMBOL>_1m_status.json``.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time as time_module
from datetime import datetime
from pathlib import Path

from config.settings import NY_TZ
from integrations.lse_one_minute import (
    LseOneMinuteSource,
    one_minute_status_path,
    write_status,
)

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("skyq.range.lse_bridge")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="SPY", help="SPY or QQQ")
    parser.add_argument(
        "--key-file",
        default=os.environ.get("LSE_API_KEY_FILE", ""),
        help="file containing the LSE API key (default: LSE_API_KEY_FILE)",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(ROOT / "data" / "lse"),
        help="bridge cache root",
    )
    parser.add_argument("--warmup-days", type=int, default=45)
    parser.add_argument("--bar-grace-seconds", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    parser.add_argument(
        "--once",
        action="store_true",
        help="start, publish one heartbeat, then stop (configuration check)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    args = _parser().parse_args(argv)
    symbol = args.symbol.upper()
    if symbol not in {"SPY", "QQQ"}:
        log.error("the deployable RANGE books are SPY and QQQ, not %s", symbol)
        return 2
    cache_root = Path(args.cache_dir).resolve()
    status_path = one_minute_status_path(cache_root, symbol)
    source = LseOneMinuteSource(
        symbol,
        key_file=Path(args.key_file).expanduser() if args.key_file else None,
        warmup_days=max(1, args.warmup_days),
        cache_dir=cache_root,
        bar_grace_seconds=max(0.0, args.bar_grace_seconds),
    )
    stopping = False

    def _stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        log.info("signal %s received; stopping bridge", signum)

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    write_status(
        status_path,
        {
            "schema": 1,
            "symbol": symbol,
            "timeframe": "1m",
            "ready": False,
            "session_date": datetime.now(tz=NY_TZ).date().isoformat(),
            "error": None,
            "message": "bridge starting; downloading warmup",
            "updated_at": datetime.now(tz=NY_TZ).isoformat(),
            "pid": os.getpid(),
        },
    )
    try:
        source.start(timeout=args.startup_timeout)
    except Exception as exc:
        payload = {
            "schema": 1,
            "symbol": symbol,
            "timeframe": "1m",
            "ready": False,
            "fatal": True,
            "error": str(exc),
            "session_date": datetime.now(tz=NY_TZ).date().isoformat(),
            "updated_at": datetime.now(tz=NY_TZ).isoformat(),
            "pid": os.getpid(),
        }
        write_status(status_path, payload)
        log.error("bridge failed to start: %s", exc)
        return 1

    log.info("bridge ready: %s", source.status())
    try:
        while not stopping:
            try:
                source.poll()
                write_status(status_path, source.status_payload())
            except Exception as exc:  # keep the heartbeat honest on transient failure
                log.exception("bridge poll failed: %s", exc)
                write_status(
                    status_path,
                    {
                        "schema": 1,
                        "symbol": symbol,
                        "timeframe": "1m",
                        "ready": False,
                        "fatal": True,
                        "error": str(exc),
                        "session_date": datetime.now(tz=NY_TZ).date().isoformat(),
                        "updated_at": datetime.now(tz=NY_TZ).isoformat(),
                        "pid": os.getpid(),
                    },
                )
                return 1
            if args.once:
                break
            time_module.sleep(max(0.25, float(args.poll_seconds)))
    finally:
        source.stop()
        try:
            final = source.status_payload()
            final["ready"] = False
            final["message"] = "bridge stopped"
            write_status(status_path, final)
        except Exception:
            pass
        log.info("bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
