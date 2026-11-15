"""MetaTrader 5 adapter (Windows only).

The terminal and Python package do not run on Linux.  The import is lazy so the
rest of the research stack, tests and dashboard stay platform-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from integrations.base import (
    IntegrationError,
    IntegrationStatus,
    env_present,
    env_value,
    missing,
)


@dataclass(frozen=True)
class MT5Config:
    path: str = ""
    login: int = 0
    password: str = ""
    server: str = ""
    timeout: int = 60_000

    @classmethod
    def from_env(cls) -> "MT5Config":
        if not env_present("MT5_LOGIN"):
            raise IntegrationError("MT5_LOGIN is not set")
        return cls(
            path=env_value("MT5_PATH"),
            login=int(env_value("MT5_LOGIN", "0")),
            password=env_value("MT5_PASSWORD"),
            server=env_value("MT5_SERVER"),
        )


def load():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:
        raise IntegrationError(
            "MetaTrader5 is Windows-only; run this adapter on the Windows terminal host"
        ) from exc
    return mt5


class MT5Terminal:
    def __init__(self, config: MT5Config | None = None) -> None:
        self.config = config or MT5Config.from_env()
        self.mt5 = load()
        self.connected = False

    def connect(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self.config.timeout}
        if self.config.path:
            kwargs["path"] = self.config.path
        if self.config.login:
            kwargs.update(
                login=self.config.login,
                password=self.config.password,
                server=self.config.server,
            )
        if not self.mt5.initialize(**kwargs):
            code, message = self.mt5.last_error()
            raise IntegrationError(f"MT5 initialize failed ({code}): {message}")
        self.connected = True
        return self.account()

    def account(self) -> dict[str, Any]:
        info = self.mt5.account_info()
        if info is None:
            raise IntegrationError("MT5 account_info() is None; log in to the terminal")
        return {
            "login": int(getattr(info, "login", 0)),
            "server": str(getattr(info, "server", "")),
            "currency": str(getattr(info, "currency", "")),
            "equity": float(getattr(info, "equity", 0.0)),
            "balance": float(getattr(info, "balance", 0.0)),
            "margin_free": float(getattr(info, "margin_free", 0.0)),
            "trade_mode": int(getattr(info, "trade_mode", 0)),
            "is_demo": int(getattr(info, "trade_mode", 0))
            != int(self.mt5.ACCOUNT_TRADE_MODE_REAL),
        }

    def close(self) -> None:
        if self.connected:
            try:
                self.mt5.shutdown()
            finally:
                self.connected = False


def status() -> IntegrationStatus:
    configured = env_present("MT5_LOGIN")
    try:
        load()
        installed = True
    except IntegrationError:
        installed = False
    names = ("MT5_PATH", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER")
    return IntegrationStatus(
        name="mt5",
        configured=configured,
        reachable=configured and installed,
        detail=(
            "Windows terminal adapter ready; use a demo account first."
            if configured and installed
            else f"Windows-only; set {missing(names)} and install MetaTrader5."
        ),
        env_vars=names,
        role="Windows CFD execution",
    )
