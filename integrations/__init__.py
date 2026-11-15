"""Optional broker and market-data integrations.

Importing this package is side-effect free.  Each adapter loads its optional
dependency and credentials lazily, and ``doctor`` only reports presence and
localhost reachability.
"""

from __future__ import annotations

from integrations import alpaca, databento, ibkr, lse, moomoo, mt5
from integrations.base import IntegrationStatus


def doctor() -> list[IntegrationStatus]:
    """Return adapter readiness without making a network or broker call."""
    checks = (
        alpaca.status,
        databento.status,
        ibkr.status,
        lse.status,
        moomoo.status,
        mt5.status,
    )
    output: list[IntegrationStatus] = []
    for check in checks:
        try:
            output.append(check())
        except Exception as exc:  # a missing optional package is not a crash
            output.append(
                IntegrationStatus(
                    name=check.__module__.rsplit(".", 1)[-1],
                    configured=False,
                    reachable=False,
                    detail=f"check failed: {type(exc).__name__}",
                    env_vars=(),
                    role="optional adapter",
                )
            )
    return output


__all__ = ["doctor", "alpaca", "databento", "ibkr", "lse", "moomoo", "mt5"]
