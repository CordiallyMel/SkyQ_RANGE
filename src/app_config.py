"""Runtime settings for the SQL/API/control layer.

The strategy's research constants live in :mod:`config.settings`; this module
contains deployment concerns only.  Secrets are read from the environment (or
an untracked ``.env``) and are never logged or returned by the API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> None:
    """Load simple KEY=VALUE lines without overriding the real environment."""
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


@dataclass(frozen=True)
class AppSettings:
    database_url: str
    host: str
    port: int
    control_enabled: bool
    control_token: str
    log_level: str
    root: Path

    @classmethod
    def from_env(cls) -> "AppSettings":
        load_dotenv()
        default_db = f"sqlite:///{(ROOT / 'var' / 'skyq_range.db').as_posix()}"
        return cls(
            database_url=os.getenv("DATABASE_URL", default_db).strip(),
            host=os.getenv("SKYQ_API_HOST", "127.0.0.1").strip(),
            port=int(os.getenv("SKYQ_API_PORT", "8000")),
            control_enabled=os.getenv("SKYQ_CONTROL_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"},
            control_token=os.getenv("SKYQ_CONTROL_TOKEN", "").strip(),
            log_level=os.getenv("SKYQ_LOG_LEVEL", "INFO").strip().upper(),
            root=ROOT,
        )


def mask_database_url(url: str) -> str:
    """A safe display form for logs and the /api/system response."""
    if "@" not in url:
        return url
    prefix, rest = url.split("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else ""
    return f"{scheme}://***@{rest}"
