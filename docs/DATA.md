# Data and SQL

## What is committed

Large parquet and full trade ledgers are deliberately excluded from Git. The
small, reviewable reporting bundle is committed:

| Path | Rows | Purpose |
|---|---:|---|
| `data/published/range_metrics.csv` | 4 | both books, two windows |
| `data/published/equity_curve.csv` | 17,816 | daily closed-trade equity |
| `data/published/recent_trades.csv` | 500 | 250 newest SPY + 250 newest QQQ |
| `data/published/futures_gate.csv` | 9 | cross-asset rejection |
| `data/published/etf_1000sh_comparison.csv` | small | source comparison |
| `data/published/provenance.json` | — | source row counts and SHA-256 hashes |

`scripts/build_published_data.py` generates the metrics, equity and recent-trade
sample from the canonical ledgers. `scripts/research_futures.py` generates the
futures gate. The generated JSON records the exact ledger hashes used.

## Data dictionary

### `equity_curve.csv`

| Column | Meaning |
|---|---|
| `window` | `2003-2026` or `2007-2026` |
| `symbol` | `SPY` or `QQQ` |
| `date` | New York session date |
| `daily_pnl` | sum of closed-trade net P&L for the date |
| `equity` | starting $1M + cumulative closed-trade net |
| `drawdown` | closed-trade drawdown from the prior equity high |

### `recent_trades.csv`

One row per closed trade. Fields include side, signal/entry/exit timestamps,
entry, stop and target prices, exit price and reason, quantity, gross P&L, costs,
net P&L, bars held, channel width and zone price. The full ledgers have the same
shape.

### `futures_gate.csv`

| Column | Meaning |
|---|---|
| `symbol` | futures root |
| `description` | contract or proxy |
| `bars` | 1-minute bars tested |
| `trades` | completed trades |
| `win_rate` | decimal fraction |
| `profit_factor` | gross profit / gross loss |
| `net`, `net_per_month`, `max_drawdown` | dollars, after costs |
| `positive_years`, `years` | yearly consistency |
| `verdict` | gate outcome |

## External source data

The full history parquet files are not part of this repository. Point the research
CLI at them:

```bash
python -m scripts.backtest_range --symbol SPY --quantity 1000 \
  --data /path/to/SPY_1m_24y.parquet --start 2003-09-10 --end 2026-11-04
```

The expected schema is a time-indexed frame with `open`, `high`, `low`, `close`
and `volume`. A `date`/`time` column is accepted and normalized. The backtester
does not consume optional indicator columns.

The futures gate expects the files listed in `scripts/research_futures.py`
(`mes_1m.parquet`, `mnq_1m.parquet`, `rty_1m_indicators.parquet`, `zt_1m.parquet`,
`es_1m.parquet`, `nq_1m.parquet`, `zn_1m.parquet`, `cl_1m.parquet`,
`gc_1m.parquet`).

## Database loading

### Seed

```bash
# SQLite fallback
DATABASE_URL=sqlite:///var/skyq_range.db python -m scripts.init_db

# PostgreSQL
DATABASE_URL=postgresql+psycopg://user:YOUR_PASSWORD@127.0.0.1:5432/skyq_range \
  python -m scripts.init_db
```

The seed is idempotent: it updates run metrics and replaces the published equity
and futures rows. It leaves existing trade rows alone, so a full ledger loaded
with `scripts/load_ledgers.py` is not silently reduced to the 250-row sample. Use
`python -m scripts.init_db --refresh-trades` if you deliberately want the
committed sample back.

### Full ledgers

The full ledgers are much larger than the committed sample. Load them once:

```bash
DATABASE_URL=... python -m scripts.load_ledgers \
  --spy /path/to/skyq_range_spy_1000sh_2003_2026_trades.csv \
  --qqq /path/to/skyq_range_qqq_1000sh_2003_2026_trades.csv
```

This replaces the sample rows for each 2003–2026 run and records an audit event.
The metrics and equity seed are unchanged.

### SQL views

`sql/002_views.sql` creates:

- `v_range_metrics` — metrics joined to instrument names for all four runs;
- `v_trade_attribution` — trades grouped by book, side and exit reason.

A fresh database can be initialized with either `scripts.init_db` or the SQL
files. If you use the SQL files, run `scripts.init_db` afterward to seed the
data.

## Provenance and reproducibility

- The canonical backtest reports are in `reports/backtests/`.
- The merged CLI applies the symbol preset automatically.
- Published metrics and the full-history reports agree to the cent for the 2003
  window (SPY net $1,658,827.85; QQQ net $484,445.10).
- The futures gate is generated from the same strategy code with a long-only,
  1-contract baseline and contract-specific commissions.

Data availability is not a pass. A missing futures file produces a `not run`
row in the gate; it is never silently treated as positive.
