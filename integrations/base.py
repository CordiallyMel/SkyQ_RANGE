"""Shared primitives for optional broker and market-data integrations."""

from __future__ import annotations

import os
import socket
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class IntegrationStatus:
    name: str
    configured: bool
    reachable: bool
    detail: str
    env_vars: tuple[str, ...]
    role: str

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["env_vars"] = list(self.env_vars)
        return payload


class IntegrationError(RuntimeError):
    """A clean integration failure that is safe to show to an operator."""


def env_present(*names: str) -> bool:
    """True only when every named variable has a non-empty value."""
    return all(bool(os.getenv(name, "").strip()) for name in names)


def env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def redact(value: str, keep: int = 4) -> str:
    """A stable display form that never exposes the full value."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}…"


def missing(names: tuple[str, ...]) -> str:
    return ", ".join(names) if names else ""
