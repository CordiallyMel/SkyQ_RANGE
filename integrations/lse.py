"""London Strategic Edge (LSE) Data 3.0 archive client.

LSE is a **data vendor, not a broker**.  This module fetches completed bars or
trade slices through the documented ``/v3/export`` job flow and never submits
orders.  A live WebSocket feed (``wss://data-ws.londonstrategicedge.com``) is a
separate forward-testing concern; see ``docs/INTEGRATIONS.md`` for the boundary.
"""

from __future__ import annotations

import io
import json
import time
from datetime import date, timedelta
from typing import Any
from urllib import error, parse, request

import pandas as pd

from integrations.base import (
    IntegrationError,
    IntegrationStatus,
    env_present,
    env_value,
)

BASE_URL = "https://download.londonstrategicedge.com/v3"
MARKET = "us_stocks"
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
EMPTY_CODES = frozenset({"data-unavailable", "pair-unknown", "date-not-trading"})


class LseError(IntegrationError):
    pass


class LseDataUnavailable(LseError):
    pass


class LseAuthError(LseError):
    pass


class LseArchive:
    """Paced, retrying GET client for the LSE archive."""

    def __init__(self, api_key: str, *, timeout: float = 120.0, poll_seconds: float = 1.0):
        if not api_key:
            raise LseError("an LSE API key is required")
        self.api_key = api_key
        self.timeout = float(timeout)
        self.poll_seconds = max(0.25, float(poll_seconds))

    @classmethod
    def from_env(cls) -> "LseArchive":
        if not env_present("LSE_API_KEY"):
            raise LseError("LSE_API_KEY is not set; add it to .env, never to Git")
        return cls(env_value("LSE_API_KEY"))

    def _open(self, path: str, params: dict[str, Any] | None = None):
        query = parse.urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        )
        url = f"{BASE_URL}/{path.lstrip('/')}"
        if query:
            url += "?" + query
        req = request.Request(
            url,
            headers={
                "X-API-Key": self.api_key,
                "Accept": "application/octet-stream, application/json",
            },
        )
        attempt = 0
        while True:
            try:
                return request.urlopen(req, timeout=self.timeout)
            except error.HTTPError as exc:
                raw = exc.read()
                try:
                    payload = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    payload = {}
                code = str(payload.get("code") or exc.code)
                message = str(payload.get("message") or exc.reason or "")
                if exc.code in RETRY_STATUS and attempt < 2:
                    retry_after = exc.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else float(2**attempt)
                    time.sleep(min(30.0, max(1.0, delay)))
                    attempt += 1
                    continue
                if code in EMPTY_CODES:
                    raise LseDataUnavailable(f"{code}: {message}") from exc
                if exc.code in {401, 403}:
                    raise LseAuthError(f"{code}: {message}") from exc
                raise LseError(f"{code}: {message}") from exc
            except error.URLError as exc:
                if attempt >= 2:
                    raise LseError(f"LSE transport failed: {exc.reason}") from exc
                time.sleep(min(15.0, 2.0**attempt))
                attempt += 1

    def _export_bytes(self, params: dict[str, Any]) -> bytes:
        with self._open("export", params) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
        if not content_type.startswith("application/json"):
            return raw
        job = json.loads(raw)
        job_id = str(job.get("job_id") or "")
        if not job_id:
            raise LseError("LSE export did not return a job id")
        deadline = time.monotonic() + 300.0
        while time.monotonic() < deadline:
            with self._open(f"jobs/{job_id}") as response:
                state = json.loads(response.read())
            if state.get("status") == "ready":
                with self._open(f"jobs/{job_id}/file") as response:
                    return response.read()
            if state.get("status") in {"failed", "expired"}:
                raise LseError(f"LSE export {job_id} {state.get('status')}")
            time.sleep(self.poll_seconds)
        raise LseError("LSE export timed out")

    def export(
        self,
        *,
        symbol: str,
        start: date,
        end: date,
        timeframe: str = "5m",
        data: str = "candles",
        fmt: str = "parquet",
    ) -> bytes:
        return self._export_bytes(
            {
                "market": MARKET,
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "data": data,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "format": fmt,
                "adjusted": 1,
            }
        )

    def bars(self, symbol: str, start: date, end: date, timeframe: str = "5m") -> pd.DataFrame:
        raw = self.export(symbol=symbol, start=start, end=end, timeframe=timeframe)
        try:
            frame = pd.read_parquet(io.BytesIO(raw))
        except Exception as exc:
            raise LseError("LSE archive returned unreadable bars") from exc
        frame.columns = [str(column).lower() for column in frame.columns]
        for candidate in ("time", "timestamp", "date", "datetime"):
            if candidate in frame.columns:
                frame[candidate] = pd.to_datetime(frame[candidate], utc=True)
                frame = frame.set_index(candidate)
                break
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise LseError("LSE bars have no time index")
        missing = [column for column in ("open", "high", "low", "close", "volume") if column not in frame]
        if missing:
            raise LseError(f"LSE bars missing columns: {', '.join(missing)}")
        return frame[["open", "high", "low", "close", "volume"]].astype(float).sort_index()

    def usage(self) -> dict[str, Any]:
        with self._open("usage") as response:
            return json.loads(response.read())


def status() -> IntegrationStatus:
    configured = env_present("LSE_API_KEY")
    return IntegrationStatus(
        name="lse",
        configured=configured,
        reachable=configured,
        detail=(
            "Archive key present; LSE is a data vendor, not an execution broker."
            if configured
            else "Set LSE_API_KEY for archive warmup and forward-test data."
        ),
        env_vars=("LSE_API_KEY",),
        role="Market data vendor / archive",
    )
