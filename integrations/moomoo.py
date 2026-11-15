"""Read-only moomoo / Futu OpenD adapter.

OpenD is a local gateway; there is no API key in this repository.  The adapter
only reads snapshots and historical bars.  It does not place orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from integrations.base import (
    IntegrationError,
    IntegrationStatus,
    env_value,
    port_open,
)


@dataclass(frozen=True)
class MoomooConfig:
    host: str = "127.0.0.1"
    port: int = 11111

    @classmethod
    def from_env(cls) -> "MoomooConfig":
        return cls(
            host=env_value("MOOMOO_HOST", "127.0.0.1"),
            port=int(env_value("MOOMOO_PORT", "11111")),
        )


class MoomooQuotes:
    def __init__(self, config: MoomooConfig | None = None) -> None:
        self.config = config or MoomooConfig.from_env()
        try:
            import futu  # type: ignore
        except ImportError as exc:
            raise IntegrationError(
                "futu-api is not installed; install the optional 'moomoo' extra"
            ) from exc
        if not port_open(self.config.host, self.config.port):
            raise IntegrationError(
                f"OpenD is not listening on {self.config.host}:{self.config.port}"
            )
        self.futu = futu
        self.context = futu.OpenQuoteContext(
            host=self.config.host, port=self.config.port
        )

    def snapshots(self, symbols: list[str]) -> list[dict[str, Any]]:
        codes = [
            symbol if "." in symbol else f"US.{symbol.upper()}"
            for symbol in symbols
        ]
        ret, data = self.context.get_market_snapshot(codes)
        if ret != self.futu.RET_OK:
            raise IntegrationError(f"moomoo snapshot failed: {data}")
        return data.to_dict(orient="records")

    def history(
        self,
        symbol: str,
        *,
        start: str,
        end: str,
        ktype: str = "K_1M",
    ) -> list[dict[str, Any]]:
        code = symbol if "." in symbol else f"US.{symbol.upper()}"
        ret, data, _ = self.context.request_history_kline(
            code,
            start=start,
            end=end,
            ktype=getattr(self.futu.KLType, ktype),
            autype=self.futu.AuType.QFQ,
            max_count=1000,
        )
        if ret != self.futu.RET_OK:
            raise IntegrationError(f"moomoo history failed: {data}")
        return data.to_dict(orient="records")

    def close(self) -> None:
        try:
            self.context.close()
        except Exception:
            pass


def status() -> IntegrationStatus:
    config = MoomooConfig.from_env()
    reachable = port_open(config.host, config.port)
    try:
        import futu  # noqa: F401

        installed = True
    except ImportError:
        installed = False
    return IntegrationStatus(
        name="moomoo",
        configured=reachable and installed,
        reachable=reachable and installed,
        detail=(
            "OpenD is listening; quotes are read-only."
            if reachable and installed
            else "Start OpenD on 127.0.0.1:11111 and install the moomoo extra."
        ),
        env_vars=("MOOMOO_HOST", "MOOMOO_PORT"),
        role="Read-only quotes / premarket data",
    )
