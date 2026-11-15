"""Interactive Brokers connection adapter.

The actual order management lives in :mod:`src.order_manager` and the RANGE
runner in :mod:`src.main`.  This module only exposes a small, testable
connection/status surface for the dashboard and doctor command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.settings import CONNECTION
from integrations.base import IntegrationError, IntegrationStatus, env_value, port_open


@dataclass(frozen=True)
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 46
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "IBKRConfig":
        return cls(
            host=env_value("IB_HOST", "127.0.0.1"),
            port=int(env_value("IB_PORT", "4002")),
            client_id=int(env_value("IB_CLIENT_ID", "46")),
            timeout=float(env_value("IB_TIMEOUT", "10")),
        )

    @property
    def paper(self) -> bool:
        return self.port in {4002, 7497}


class IBKRConnection:
    def __init__(self, config: IBKRConfig | None = None) -> None:
        self.config = config or IBKRConfig.from_env()

    def connect(self) -> Any:
        try:
            from ib_async import IB
        except ImportError as exc:
            raise IntegrationError("ib_async is not installed") from exc
        if not port_open(self.config.host, self.config.port):
            raise IntegrationError(
                f"no IB Gateway/TWS on {self.config.host}:{self.config.port}"
            )
        ib = IB()
        ib.connect(
            self.config.host,
            self.config.port,
            clientId=self.config.client_id,
            timeout=self.config.timeout,
            readonly=True,
        )
        return ib

    def account_summary(self) -> dict[str, Any]:
        ib = self.connect()
        try:
            values: dict[str, Any] = {}
            for row in ib.accountSummary():
                values[str(row.tag)] = row.value
            return values
        finally:
            ib.disconnect()


def status() -> IntegrationStatus:
    config = IBKRConfig.from_env()
    reachable = port_open(config.host, config.port)
    detail = (
        f"Gateway/TWS is listening on {config.host}:{config.port} "
        f"({'paper' if config.paper else 'live port'})."
        if reachable
        else f"Start the {'paper ' if config.paper else ''}Gateway on {config.host}:{config.port}."
    )
    return IntegrationStatus(
        name="ibkr",
        configured=bool(CONNECTION),
        reachable=reachable,
        detail=detail,
        env_vars=("IB_HOST", "IB_PORT", "IB_CLIENT_ID", "IB_ACCOUNT"),
        role="Primary paper execution bridge",
    )
