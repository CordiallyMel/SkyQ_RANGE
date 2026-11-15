"""Loopback control plane for the local dashboard.

There is deliberately no arbitrary-command endpoint.  The only mutation is
starting/stopping a fixed **paper monitor** command (``python -m src.main``
without ``--execute``), and the child PID is persisted so a restart cannot
guess at an unrelated process.  Live order submission is not exposed here.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.app_config import AppSettings

log = logging.getLogger("skyq.range.control")


class ControlError(RuntimeError):
    pass


class ControlManager:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or AppSettings.from_env()
        self.root = self.settings.root
        self.state_file = self.root / "state" / "control.json"
        self.log_dir = self.root / "logs"

    def _read_state(self) -> dict:
        if not self.state_file.exists():
            return {}
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, payload: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @staticmethod
    def _alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def status(self) -> dict:
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        alive = self._alive(pid)
        return {
            "enabled": self.settings.control_enabled,
            "running": alive,
            "pid": pid if alive else None,
            "symbol": state.get("symbol") if alive else None,
            "mode": state.get("mode") if alive else None,
            "started_at": state.get("started_at") if alive else None,
            "log_file": state.get("log_file") if alive else None,
            "last_action": state.get("last_action"),
            "last_action_at": state.get("last_action_at"),
            "note": (
                "Fixed paper-monitor command only; no order endpoint."
                if self.settings.control_enabled
                else "Control is disabled. Set SKYQ_CONTROL_ENABLED=true to enable the local paper monitor."
            ),
        }

    def start(self, symbol: str) -> dict:
        if not self.settings.control_enabled:
            raise ControlError("control is disabled")
        symbol = symbol.upper()
        if symbol not in {"SPY", "QQQ"}:
            raise ControlError("only SPY and QQQ are deployable RANGE books")
        current = self.status()
        if current["running"]:
            return current
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_file = self.log_dir / f"control-monitor-{symbol}-{stamp}.log"
        cmd = [
            sys.executable,
            "-m",
            "src.main",
            "--symbol",
            symbol,
            "--port",
            "4002",
            "--log-file",
            str(log_file),
        ]
        # The deliberate absence of --execute is the safety boundary.
        handle = log_file.open("ab")
        proc = subprocess.Popen(
            cmd,
            cwd=str(self.root),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        state = {
            "pid": proc.pid,
            "symbol": symbol,
            "mode": "paper-monitor",
            "started_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "log_file": str(log_file.relative_to(self.root)),
            "last_action": "start",
            "last_action_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        }
        self._write_state(state)
        log.info("started paper monitor pid=%s symbol=%s", proc.pid, symbol)
        return self.status()

    def stop(self) -> dict:
        if not self.settings.control_enabled:
            raise ControlError("control is disabled")
        state = self._read_state()
        pid = int(state.get("pid") or 0)
        if pid <= 0 or not self._alive(pid):
            state["last_action"] = "stop-noop"
            state["last_action_at"] = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
            self._write_state(state)
            return self.status()
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            raise ControlError(f"could not stop pid {pid}: {exc}") from exc
        state["last_action"] = "stop"
        state["last_action_at"] = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
        self._write_state(state)
        log.info("stopped paper monitor pid=%s", pid)
        return self.status()
