from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from scripts.init_db import (
    _seed_equity,
    _seed_futures,
    _seed_instruments,
    _seed_metrics,
    _seed_recent_trades,
)
from src.api import app
from src.database import init_db, make_engine, session_factory


@pytest.fixture(scope="module", autouse=True)
def seeded_database():
    engine = make_engine()
    init_db(engine)
    factory = session_factory(engine)
    with factory() as session:
        _seed_instruments(session)
        _seed_metrics(session)
        session.commit()
        _seed_equity(session)
        _seed_recent_trades(session)
        _seed_futures(session)
        session.commit()
    yield


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def test_health_and_books(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["database"] is True

    books = client.get("/api/books")
    assert books.status_code == 200
    payload = books.json()
    assert len(payload) == 4
    assert {row["symbol"] for row in payload} == {"SPY", "QQQ"}
    spy = next(row for row in payload if row["symbol"] == "SPY" and row["window"] == "2003-2026")
    assert spy["metrics"]["trades"] == 67271
    assert spy["metrics"]["net"] == pytest.approx(1_658_827.852, rel=1e-9)


def test_overview_and_trades(client: TestClient) -> None:
    overview = client.get("/api/overview", params={"symbol": "SPY", "window": "2003-2026"})
    assert overview.status_code == 200
    body = overview.json()
    assert body["symbol"] == "SPY"
    assert len(body["equity"]) > 100
    assert body["metrics"]["profit_factor"] > 1
    assert body["equity"][0]["equity"] == pytest.approx(1_000_140.0)

    trades = client.get(
        "/api/trades",
        params={"symbol": "QQQ", "window": "2003-2026", "limit": 5, "offset": 0},
    )
    assert trades.status_code == 200
    tape = trades.json()
    assert tape["total"] == 250
    assert len(tape["rows"]) == 5
    assert all(row["side"] in {"LONG", "SHORT"} for row in tape["rows"])


def test_runner_catalog_has_no_secret_values(client: TestClient) -> None:
    response = client.get("/api/runners")
    assert response.status_code == 200
    body = response.json()
    assert body["config_dir"] == "configs/runners"
    ids = {row["runner_id"] for row in body["runners"]}
    assert {"spy-1m-paper", "qqq-1m-paper"} <= ids
    spy = next(row for row in body["runners"] if row["runner_id"] == "spy-1m-paper")
    assert spy["quantity"] == 1000
    assert spy["broker"] == "simulated"
    assert "LSE_API_KEY" not in response.text
    assert "ALPACA_SECRET_KEY" in response.text  # env name, never a value


def test_futures_gate_is_negative(client: TestClient) -> None:
    response = client.get("/api/research/futures")
    assert response.status_code == 200
    rows = response.json()
    assert {row["symbol"] for row in rows} == {
        "MES", "MNQ", "M2K", "ZT", "ES", "NQ", "ZN", "CL", "GC", "YM"
    }
    assert all(row["net_per_month"] < 0 for row in rows)
    assert all(row["verdict"] == "negative after costs" for row in rows)


def test_system_does_not_leak_secrets(client: TestClient) -> None:
    response = client.get("/api/system")
    assert response.status_code == 200
    body = response.json()
    assert body["control"]["enabled"] is False
    assert "password" not in response.text.lower()
    assert "secret" not in response.text.lower()
    assert "api_key" not in response.text.lower()


def test_control_is_disabled_by_default(client: TestClient) -> None:
    response = client.post(
        "/api/control/start",
        json={"symbol": "SPY"},
        headers={"X-SkyQ-Control": "1"},
    )
    assert response.status_code == 403
    status = client.get("/api/control/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is False
