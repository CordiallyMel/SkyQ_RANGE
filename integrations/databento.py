"""Budget-guarded Databento historical adapter.

Databento bills by uncompressed delivered bytes.  The quote is obtained before
the download, and the download refuses to start when the quote exceeds the
caller's budget.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from integrations.base import (
    IntegrationError,
    IntegrationStatus,
    env_present,
)

DATASET = "EQUS.MINI"
SCHEMA = "ohlcv-1m"


@dataclass(frozen=True)
class DatabentoQuote:
    dataset: str
    schema: str
    start: str
    end: str
    symbols: str
    billable_bytes: int
    cost_usd: float
    budget_usd: float

    @property
    def within_budget(self) -> bool:
        return self.cost_usd <= self.budget_usd

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _client():
    try:
        import databento as db
    except ImportError as exc:
        raise IntegrationError(
            "databento is not installed; install the optional 'databento' extra"
        ) from exc
    key = os.getenv("DATABENTO_API_KEY", "").strip()
    if not key:
        raise IntegrationError("DATABENTO_API_KEY is not set")
    try:
        return db, db.Historical(key=key)
    except Exception as exc:
        raise IntegrationError("Databento client construction failed") from exc


def clean_symbols(symbols: list[str] | str | None) -> list[str] | None:
    if symbols is None:
        return None
    raw = symbols.split(",") if isinstance(symbols, str) else symbols
    cleaned = sorted({str(symbol).strip().upper() for symbol in raw if str(symbol).strip()})
    return None if not cleaned or cleaned == ["ALL"] else cleaned


def quote(
    start: date,
    end: date,
    *,
    symbols: list[str] | str | None = None,
    budget: float = 70.0,
) -> DatabentoQuote:
    _, client = _client()
    requested = clean_symbols(symbols)
    try:
        billable = client.metadata.get_billable_size(
            DATASET,
            start.isoformat(),
            end.isoformat(),
            symbols=requested,
            schema=SCHEMA,
            stype_in="raw_symbol",
        )
        cost = client.metadata.get_cost(
            DATASET,
            start.isoformat(),
            end.isoformat(),
            symbols=requested,
            schema=SCHEMA,
            stype_in="raw_symbol",
        )
    except Exception as exc:
        raise IntegrationError("Databento quote failed") from exc
    return DatabentoQuote(
        dataset=DATASET,
        schema=SCHEMA,
        start=start.isoformat(),
        end=end.isoformat(),
        symbols=",".join(requested) if requested else "ALL_SYMBOLS",
        billable_bytes=int(billable),
        cost_usd=float(cost),
        budget_usd=float(budget),
    )


def download(
    start: date,
    end: date,
    output_dir: str | Path,
    *,
    symbols: list[str] | str | None = None,
    budget: float = 70.0,
    force: bool = False,
) -> tuple[Path, DatabentoQuote, bool]:
    quoted = quote(start, end, symbols=symbols, budget=budget)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "bars.dbn.zst"
    if path.exists() and not force:
        return path, quoted, True
    if not quoted.within_budget:
        raise IntegrationError(
            f"Databento quote ${quoted.cost_usd:,.2f} exceeds ${quoted.budget_usd:,.2f}; "
            "no request was submitted"
        )
    db, client = _client()
    requested = clean_symbols(symbols)
    try:
        client.timeseries.get_range(
            DATASET,
            start=start.isoformat(),
            end=end.isoformat(),
            symbols=requested,
            schema=SCHEMA,
            stype_in="raw_symbol",
            stype_out="instrument_id",
            path=str(path),
        )
    except Exception as exc:
        raise IntegrationError("Databento download failed") from exc
    return path, quoted, False


def status() -> IntegrationStatus:
    configured = env_present("DATABENTO_API_KEY")
    try:
        import databento  # noqa: F401

        installed = True
    except ImportError:
        installed = False
    return IntegrationStatus(
        name="databento",
        configured=configured,
        reachable=configured and installed,
        detail=(
            "Historical client available; quote before every paid download."
            if configured and installed
            else "Set DATABENTO_API_KEY and install the databento extra."
        ),
        env_vars=("DATABENTO_API_KEY", "DATABENTO_DATASET"),
        role="Historical market data",
    )
