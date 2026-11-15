"""Run one paper book against the shared one-minute LSE bridge.

The bridge owns the WebSocket.  This process reads its warmup, live cache and
heartbeat, produces signals, and fills the deterministic paper engine.  It
never needs the LSE key, so several runners can share one free-plan connection.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time as time_module
from datetime import datetime
from pathlib import Path

from config.settings import NY_TZ
from integrations.lse_feed import LseFeedError
from integrations.lse_one_minute import SharedLseOneMinuteSource
from src.paper_session import PaperSession

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("skyq.range.lse_runner")


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(ROOT / "configs" / "runners" / "spy-1m-paper.json"),
        help="runner JSON config",
    )
    parser.add_argument("--cache-dir", default=str(ROOT / "data" / "lse"))
    parser.add_argument("--journal-dir", default=str(ROOT / "data" / "forward"))
    parser.add_argument("--quantity", type=int, default=None)
    parser.add_argument(
        "--broker",
        default=None,
        help="simulated is implemented in this public runner; see docs/INTEGRATIONS.md",
    )
    parser.add_argument("--startup-timeout", type=float, default=45.0)
    parser.add_argument(
        "--allow-degraded",
        action="store_true",
        help="keep running when the bridge reports missing ticks (not recommended)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="read the shared cache once, write a snapshot, and exit",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        help="optional bound for a smoke test",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    args = _parser().parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if args.quantity is not None:
        payload["quantity"] = args.quantity
    if args.broker is not None:
        payload["broker"] = args.broker
    broker = str(payload.get("broker", "simulated"))
    if broker != "simulated":
        log.error(
            "broker %r is not implemented by this public runner. The tested path "
            "is simulated paper; Alpaca paper order wiring is separate and "
            "requires your own paper account. See docs/INTEGRATIONS.md.",
            broker,
        )
        return 2
    try:
        session = PaperSession.from_config(payload)
    except (ValueError, KeyError) as exc:
        log.error("bad runner config %s: %s", config_path, exc)
        return 2
    symbol = session.settings.symbol
    source = SharedLseOneMinuteSource(symbol, cache_dir=args.cache_dir)
    journal_dir = Path(args.journal_dir).resolve()
    journal_dir.mkdir(parents=True, exist_ok=True)
    journal_path = journal_dir / f"{session.settings.runner_id}.csv"
    status_path = (
        journal_dir / f"{session.settings.runner_id}.status.json"
    )
    stopping = False

    def _stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        log.info("signal %s received; stopping runner", signum)

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    try:
        source.start(timeout=args.startup_timeout)
    except LseFeedError as exc:
        log.error("shared one-minute bridge is not ready: %s", exc)
        return 2

    log.info("runner %s started | %s", session.settings.runner_id, source.status())
    frame = source.frame()
    if frame.empty:
        log.error("shared bridge has no completed one-minute bars")
        return 2
    session.seed(frame)
    session.journal_frame().to_csv(journal_path, index=False)
    started = time_module.monotonic()
    try:
        while not stopping:
            if args.max_seconds and time_module.monotonic() - started >= args.max_seconds:
                break
            try:
                source.poll()
                new_frame = source.frame()
                session.sync(new_frame)
                if source.degraded_reason:
                    message = f"shared bridge degraded: {source.degraded_reason}"
                    log.error(message)
                    if not args.allow_degraded:
                        return 3
                if session.trades:
                    session.journal_frame().to_csv(journal_path, index=False)
                status = {
                    "runner": session.snapshot(),
                    "source": source.status_payload(),
                    "journal": str(journal_path),
                    "updated_at": datetime.now(tz=NY_TZ).isoformat(),
                }
                _atomic_json(status_path, status)
                log.info(
                    "%s | %s | trades=%d net=%.2f | %s",
                    session.settings.runner_id,
                    session.last_bar_time,
                    session.metrics.trades,
                    session.metrics.net_pnl,
                    "FLAT" if session.is_flat else "IN POSITION",
                )
            except Exception as exc:
                log.exception("runner poll failed: %s", exc)
                return 1
            if args.once:
                break
            time_module.sleep(max(1.0, float(session.settings.poll_seconds)))
    finally:
        if session.trades:
            session.journal_frame().to_csv(journal_path, index=False)
        _atomic_json(
            status_path,
            {
                "runner": session.snapshot(),
                "source": source.status_payload(),
                "journal": str(journal_path),
                "updated_at": datetime.now(tz=NY_TZ).isoformat(),
                "stopped": True,
            },
        )
        log.info(
            "runner stopped | trades=%d net=%.2f journal=%s",
            session.metrics.trades,
            session.metrics.net_pnl,
            journal_path,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
