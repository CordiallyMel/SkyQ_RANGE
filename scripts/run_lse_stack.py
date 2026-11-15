"""Start shared LSE bridges and one or more paper runners.

Typical command::

    python -m scripts.run_lse_stack --configs \
      configs/runners/spy-1m-paper.json configs/runners/qqq-1m-paper.json \
      --key-file ~/.config/lse/api-key

Each bridge opens one LSE WebSocket.  All runners reading the same symbol use
that bridge; no runner needs the key.  On LSE's free plan, one API key is
limited to one concurrent connection, so run one symbol at a time unless the
plan permits more.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time as time_module
from pathlib import Path

from integrations.lse_one_minute import one_minute_status_path

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger("skyq.range.lse_stack")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--configs",
        nargs="+",
        default=[
            str(ROOT / "configs" / "runners" / "spy-1m-paper.json"),
            str(ROOT / "configs" / "runners" / "qqq-1m-paper.json"),
        ],
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("LSE_API_KEY_FILE", ""),
        help="LSE key file passed to every bridge",
    )
    parser.add_argument("--cache-dir", default=str(ROOT / "data" / "lse"))
    parser.add_argument("--journal-dir", default=str(ROOT / "data" / "forward"))
    parser.add_argument("--log-dir", default=str(ROOT / "data" / "forward" / "logs"))
    parser.add_argument(
        "--no-bridges",
        action="store_true",
        help="start runners against an already-running bridge",
    )
    parser.add_argument(
        "--no-runners",
        action="store_true",
        help="start bridges only",
    )
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=0.0,
        help="optional bounded smoke run",
    )
    return parser


def _read_configs(paths: list[str]) -> list[tuple[Path, dict[str, object]]]:
    configs = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        configs.append((path, json.loads(path.read_text(encoding="utf-8"))))
    return configs


def _spawn(command: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    log.info("starting: %s -> %s", " ".join(command), log_path)
    return subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
    )


def _wait_for_bridge(symbol: str, cache_dir: str, timeout: float = 120.0) -> bool:
    status = one_minute_status_path(cache_dir, symbol)
    deadline = time_module.monotonic() + timeout
    while time_module.monotonic() < deadline:
        if status.is_file():
            try:
                payload = json.loads(status.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                payload = {}
            if payload.get("fatal"):
                log.error("bridge %s failed: %s", symbol, payload.get("error"))
                return False
            if payload.get("ready"):
                log.info("bridge %s ready", symbol)
                return True
        time_module.sleep(0.5)
    log.error("bridge %s did not become ready at %s", symbol, status)
    return False


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    args = _parser().parse_args(argv)
    configs = _read_configs(args.configs)
    symbols = sorted(
        {str(payload.get("symbol", "")).upper() for _, payload in configs}
    )
    if not symbols or "" in symbols:
        log.error("each runner config needs a symbol")
        return 2
    if not args.key_file and not args.no_bridges:
        log.error("--key-file or LSE_API_KEY_FILE is required to start bridges")
        return 2
    log_dir = Path(args.log_dir).resolve()
    processes: list[tuple[str, subprocess.Popen]] = []
    stopping = False

    def _stop(signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True
        log.info("signal %s received; stopping stack", signum)

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    try:
        if not args.no_bridges:
            for symbol in symbols:
                command = [
                    sys.executable,
                    "-m",
                    "scripts.run_lse_bridge",
                    "--symbol",
                    symbol,
                    "--cache-dir",
                    args.cache_dir,
                ]
                if args.key_file:
                    command.extend(["--key-file", args.key_file])
                processes.append(
                    (
                        f"bridge-{symbol}",
                        _spawn(command, log_dir / f"bridge-{symbol}.log"),
                    )
                )
            for symbol in symbols:
                if not _wait_for_bridge(symbol, args.cache_dir):
                    return 3

        if not args.no_runners:
            for config_path, payload in configs:
                command = [
                    sys.executable,
                    "-m",
                    "scripts.run_lse_runner",
                    "--config",
                    str(config_path),
                    "--cache-dir",
                    args.cache_dir,
                    "--journal-dir",
                    args.journal_dir,
                ]
                runner_id = str(payload.get("runner_id", "runner"))
                processes.append(
                    (
                        runner_id,
                        _spawn(command, log_dir / f"{runner_id}.log"),
                    )
                )

        started = time_module.monotonic()
        while not stopping:
            for name, process in processes:
                code = process.poll()
                if code is not None:
                    log.error("%s exited with code %s", name, code)
                    return int(code) if code else 1
            if args.max_seconds and time_module.monotonic() - started >= args.max_seconds:
                break
            time_module.sleep(1.0)
    finally:
        for name, process in reversed(processes):
            if process.poll() is None:
                log.info("stopping %s (pid %s)", name, process.pid)
                process.terminate()
        deadline = time_module.monotonic() + 10.0
        for _name, process in processes:
            remaining = max(0.0, deadline - time_module.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
