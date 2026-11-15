# Operations runbook

## One-time setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
```

Use a dedicated database role and a local-only password. Do not use a PostgreSQL
superuser for the application. The example below creates a normal role:

```sql
CREATE ROLE skyq LOGIN PASSWORD 'choose-a-local-password';
CREATE DATABASE skyq_range OWNER skyq;
```

Set the URL in `.env`:

```text
DATABASE_URL=postgresql+psycopg://skyq:choose-a-local-password@127.0.0.1:5432/skyq_range
```

SQLite fallback:

```text
DATABASE_URL=sqlite:///var/skyq_range.db
```

## Seed and run

```bash
python -m scripts.init_db
python -m scripts.run_api
```

The API runs on `127.0.0.1:8000` by default and serves the built dashboard from
`dashboard/dist`. Build it once:

```bash
cd dashboard
npm ci
npm run build
```

For frontend iteration use `npm run dev`; Vite proxies `/api` to port 8000. The
dashboard is not a separate production service; it is static output served by
FastAPI (or any static host with an API base URL).

## Database operations

```bash
# Schema and views
psql -d skyq_range -f sql/001_schema.sql
psql -d skyq_range -f sql/002_views.sql

# Re-seed the published reporting sample (idempotent; keeps existing trades)
python -m scripts.init_db

# Deliberately restore the committed 500-row trade sample
python -m scripts.init_db --refresh-trades

# Load full ledgers after a backtest
python -m scripts.load_ledgers --spy ... --qqq ...
```

`init_db` is safe to rerun. It updates the four run metrics and replaces the
published equity and futures rows. It does not replace existing trade rows, so a
full ledger loaded with `scripts.load_ledgers` survives a reseed. Pass
`--refresh-trades` only when you want the committed 500-row sample restored.

## Control endpoint

```text
GET  /api/control/status
POST /api/control/start
POST /api/control/stop
```

Requirements:

- server bound to loopback;
- `SKYQ_CONTROL_ENABLED=true`;
- header `X-SkyQ-Control: 1`;
- `X-SkyQ-Control-Token: <token>` when `SKYQ_CONTROL_TOKEN` is set.

The start action runs exactly one allowlisted command:
`python -m src.main --symbol SPY --port 4002` (no `--execute`). It is a monitor,
not an order launcher. Stop sends a terminate signal to the recorded PID.

Example:

```bash
curl -X POST http://127.0.0.1:8000/api/control/start \
  -H 'Content-Type: application/json' \
  -H 'X-SkyQ-Control: 1' \
  -H "X-SkyQ-Control-Token: $SKYQ_CONTROL_TOKEN" \
  -d '{"symbol":"SPY"}'
```

Never expose this port publicly with control enabled. The API has no multi-user
authentication.

## Health checks

```bash
curl -s http://127.0.0.1:8000/api/health
curl -s http://127.0.0.1:8000/api/system
python -m scripts.check_integrations
pytest -q
```

`/api/health` reports API and database availability. `/api/system` reports
counts, a masked database URL, package versions and the control state.

## Logs

- API: stdout of `scripts/run_api.py`; use your process manager's log file.
- Control monitor: `logs/control-monitor-*.log` plus its state file in `state/`.
- Full ledger load: redirect to a file and check the exit status before assuming
  success.
- Backtest: `reports/backtests/` and the console output.

## Troubleshooting

| Symptom | Check |
|---|---|
| API says database unavailable | `DATABASE_URL`, PostgreSQL service, role privileges |
| Dashboard blank but API healthy | `dashboard/dist/index.html` exists; rebuild |
| `/api/control/start` returns 403 | control disabled, non-loopback client, missing header/token |
| 409 on control start | monitor already running; stop it first |
| `ModuleNotFoundError: ib_async` | `pip install -e .` (core dependency) |
| MT5 import fails on Linux | Windows-only adapter; expected |
| futures gate shows missing files | point `--data-root` at the external parquet directory |
| `psql` not installed | use `scripts.init_db`; psql is optional |

## Backup

The committed CSV/JSON bundle is the portable backup of the published research.
For PostgreSQL, use `pg_dump` with the application role. Never commit a dump
containing account data or credentials.
