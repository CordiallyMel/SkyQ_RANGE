"""Database engine and schema initialization."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.app_config import AppSettings
from src.models import Base


def make_engine(url: str | None = None) -> Engine:
    settings = AppSettings.from_env()
    target = url or settings.database_url
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if target.startswith("sqlite"):
        if target.startswith("sqlite:///"):
            path = Path(target.removeprefix("sqlite:///"))
            # A POSIX absolute URL is sqlite:////tmp/...; Path handles both
            # that and Windows drive paths.  Create the parent so a fresh
            # checkout does not depend on a committed var/ directory.
            if str(path) != ":memory:" and path.parent and str(path.parent) not in {"", "."}:
                path.parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(target, **kwargs)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
