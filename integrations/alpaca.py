"""Paper-only Alpaca adapter.

This is intentionally not wired to the live RANGE runner.  It gives operators a
small, auditable bridge for a dedicated Alpaca paper account and refuses any
base URL other than the paper endpoint.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from integrations.base import (
    IntegrationError,
    IntegrationStatus,
    env_present,
    env_value,
    missing,
)

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"


@dataclass(frozen=True)
class AlpacaConfig:
    api_key: str
    secret_key: str
    timeout: float = 15.0

    @classmethod
    def from_env(cls) -> "AlpacaConfig":
        names = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
        if not env_present(*names):
            raise IntegrationError(f"missing {missing(names)}")
        return cls(env_value("ALPACA_API_KEY"), env_value("ALPACA_SECRET_KEY"))


class AlpacaPaper:
    """Minimal paper client; POST requests are never retried."""

    def __init__(self, config: AlpacaConfig | None = None) -> None:
        self.config = config or AlpacaConfig.from_env()
        self.base_url = PAPER_BASE_URL
        self.data_url = DATA_BASE_URL

    @property
    def is_paper(self) -> bool:
        return self.base_url == PAPER_BASE_URL

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.config.api_key,
            "APCA-API-SECRET-KEY": self.config.secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "SkyQ-RANGE-Alpaca/1.0",
        }

    def _call(
        self,
        method: str,
        path: str,
        *,
        data_url: bool = False,
        body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        retry: bool = True,
    ) -> Any:
        base = self.data_url if data_url else self.base_url
        url = f"{base}/{path.lstrip('/')}"
        if params:
            url += "?" + parse.urlencode(params)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(url, data=payload, headers=self._headers(), method=method)
        attempts = 2 if retry and method in {"GET", "DELETE"} else 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                with request.urlopen(req, timeout=self.config.timeout) as response:
                    raw = response.read()
                    return json.loads(raw.decode("utf-8")) if raw else {}
            except error.HTTPError as exc:
                raw = exc.read()
                try:
                    detail = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    detail = {}
                message = str(detail.get("message") or detail.get("error") or exc.reason)
                last = IntegrationError(f"Alpaca HTTP {exc.code}: {message}")
                if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 >= attempts:
                    raise last from exc
            except error.URLError as exc:
                last = IntegrationError(f"Alpaca transport failure: {exc.reason}")
                if attempt + 1 >= attempts:
                    raise last from exc
        raise last or IntegrationError("Alpaca request failed")

    def account(self) -> dict[str, Any]:
        return self._call("GET", "/v2/account")

    def clock(self) -> dict[str, Any]:
        return self._call("GET", "/v2/clock")

    def positions(self) -> list[dict[str, Any]]:
        payload = self._call("GET", "/v2/positions")
        return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []

    def latest_quote(self, symbol: str, feed: str = "iex") -> dict[str, Any]:
        symbol = symbol.upper()
        payload = self._call(
            "GET",
            f"/v2/stocks/{parse.quote(symbol)}/quotes/latest",
            data_url=True,
            params={"feed": feed},
        )
        quote = payload.get("quote") if isinstance(payload, dict) else None
        if not isinstance(quote, dict):
            raise IntegrationError(f"Alpaca returned no quote for {symbol}")
        return {"symbol": symbol, "feed": feed, **quote}

    def submit_bracket(
        self,
        *,
        symbol: str,
        side: str,
        qty: int,
        stop_price: float,
        target_price: float,
        client_order_id: str,
    ) -> dict[str, Any]:
        side = side.strip().lower()
        if side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        if qty <= 0 or stop_price <= 0 or target_price <= 0:
            raise ValueError("qty, stop_price and target_price must be positive")
        body = {
            "symbol": symbol.upper(),
            "qty": str(int(qty)),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "position_intent": "buy_to_open" if side == "buy" else "sell_to_open",
            "client_order_id": client_order_id,
            "take_profit": {"limit_price": f"{target_price:.2f}"},
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
        }
        return self._call("POST", "/v2/orders", body=body, retry=False)

    def close_position(self, symbol: str) -> dict[str, Any]:
        payload = self._call("DELETE", f"/v2/positions/{parse.quote(symbol.upper())}")
        return payload if isinstance(payload, dict) else {}


def status() -> IntegrationStatus:
    names = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    configured = env_present(*names)
    return IntegrationStatus(
        name="alpaca",
        configured=configured,
        reachable=configured,
        detail=(
            "Paper endpoint configured; credentials are read from the environment."
            if configured
            else f"Set {missing(names)} for paper execution."
        ),
        env_vars=names,
        role="Paper equity execution",
    )
