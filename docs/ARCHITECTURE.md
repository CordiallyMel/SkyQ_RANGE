# Architecture

## Data flow

```text
1-minute OHLCV (parquet, external)
        │
        ▼
src/indicators.py          causal features, completed-bar gate, session filter
        │
        ▼
src/strategy_logic.py      channel lifecycle, entry evaluation, bracket plans
        │
        ├──────────────► src/backtest.py       conservative next-open fill + stop-first
        │                       │
        │                       ▼
        │                trade ledger CSV ──► data/published/*.csv
        │                       │
        │                       ▼
        │                src/models.py ──► PostgreSQL / SQLite
        │                       │
        │                       ▼
        │                src/api.py ──► FastAPI ──► dashboard/dist (React)
        │
        └──────────────► src/main.py          IBKR paper runner (SPY/QQQ only)
                                │
                                └──► src/broker_connection.py
                                     src/order_manager.py
```

The dashboard never computes strategy results. It reads a SQL-backed report
model, so the same numbers are available to SQL analysts and the UI.

## Layers

| Layer | Modules | Responsibility |
|---|---|---|
| Configuration | `config/settings.py` | instruments, presets, causal constants, risk caps |
| Research | `src/indicators.py`, `src/strategy_logic.py`, `src/backtest.py` | bars → channels → trades |
| Execution | `src/main.py`, `src/broker_connection.py`, `src/order_manager.py` | IBKR monitor / paper order lifecycle |
| Forward paper | `src/paper_session.py`, `scripts/run_lse_runner.py` | deterministic next-open paper book + journal |
| LSE data | `integrations/lse_one_minute.py`, `scripts/run_lse_bridge.py` | one WebSocket, 1m warmup/live cache, shared reader |
| Reporting | `src/models.py`, `src/database.py`, `scripts/init_db.py` | normalized SQL schema and seed |
| API | `src/api.py`, `src/app_config.py` | read model and masked system status |
| Control | `src/control.py` | one fixed paper-monitor command, loopback only |
| UI | `dashboard/` | React/Vite/TypeScript control center |
| Optional adapters | `integrations/` | IBKR, Alpaca paper, MT5, moomoo, Databento, LSE |

## SQL schema

```text
instruments(symbol PK)
    │
    └──< backtest_runs(id PK, symbol FK, strategy, window)
              │
              ├── backtest_metrics(run_id PK/FK)
              ├──< equity_points(run_id FK, date, equity, drawdown)
              └──< trades(run_id FK, side, entry/exit, net_pnl, ...)

futures_gate(symbol PK, net, profit_factor, verdict, ...)
audit_events(id PK, created_at, action, detail JSON)
market_bars(dataset, symbol, ts PK, OHLCV, source)   -- optional, separate
```

`sql/001_schema.sql` is the PostgreSQL DDL. `scripts/init_db.py` creates the same
schema through SQLAlchemy and seeds the committed sample. `sql/002_views.sql`
provides `v_range_metrics` and `v_trade_attribution`.

## API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | database and API liveness |
| GET | `/api/books` | all four book windows with metrics |
| GET | `/api/overview` | one book, downsampled equity, monthly P&L |
| GET | `/api/trades` | paginated closed trades, side filter |
| GET | `/api/research/futures` | cross-asset gate evidence |
| GET | `/api/system` | masked database info, packages, control state, audit |
| GET | `/api/runners` | safe catalog of `configs/runners/*.json` |
| GET | `/api/integrations` | optional adapters: configured/reachable names only |
| GET | `/api/control/status` | paper monitor state |
| POST | `/api/control/start` | start the fixed `src.main` paper monitor |
| POST | `/api/control/stop` | stop the recorded PID |

The API is read-only by default. The two control routes are disabled unless
`SKYQ_CONTROL_ENABLED=true`, verify a loopback client, require
`X-SkyQ-Control: 1`, and optionally require `SKYQ_CONTROL_TOKEN`.

## Failure boundaries

- **Missing data file:** research CLI exits non-zero with a clear path error.
- **Missing database:** API startup fails rather than serving fabricated data.
- **Missing broker:** the runner logs and exits; the dashboard remains usable.
- **Futures symbol in live runner:** rejected before any broker connection.
- **Control disabled:** returns 403; no process is started.
- **Stale LSE bridge:** shared source refuses the session; the runner exits 2/3
  rather than trading a stale or partial tape.
- **Optional package missing:** integration doctor reports `not ready`; imports
  are lazy so the core stays installable without broker packages.

## Forward data flow

```text
LSE ticks ──► 1m builder ──► live CSV ─┐
LSE archive ──► warmup parquet ────────┼──► SharedLseOneMinuteSource
                     status JSON ─────┘             │
                                                   ▼
                                        PaperSession (one per runner)
                                                   │
                                     journal CSV + status JSON
```

The bridge is the only process with a key. The runner never opens a socket and
never submits a live order.

## Why one engine, two presets

The two former repositories duplicated the same strategy with different stop,
channel-floor and time-stop values. Duplication created a real operational risk:
a change applied to one copy could silently diverge from the other. The merged
repository owns one engine and a `BookPreset` per ETF symbol. SPY and QQQ cannot
accidentally share the wrong stop or hold period.

The futures symbols are in the instrument registry for research reproducibility,
but `LIVE_SYMBOLS` contains only SPY and QQQ.
