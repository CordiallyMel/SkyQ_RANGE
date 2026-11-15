# Data for backtests

This repository ships the **code, compact published results, and a catalog** of
the research archive. It does not commit the raw multi-gigabyte market files:
GitHub is not a market-data warehouse, vendor licences differ, and a 5 GB
history would make a normal `git clone` unusable.

The archive inventory is in [`catalog.json`](catalog.json). It covers:

- SPY and QQQ 1-minute and 5-minute books, including the canonical 24-year files
- 1-minute futures for MES, MNQ, NQ, YM, ES, ZT, ZN, CL, GC and RTY proxies
- leveraged ETF extensions (SSO, TQQQ)
- SPY/QQQ option underlying bars, 1m/5m option panels and option research ledgers
- Databento stock scanner caches and raw archives
- forex/crypto 1-minute context files and the macro-event calendars

The exact local paths and sizes are deliberately relative. The owner's archive
is split across `Data_For_Backtests/`, `SkyQ_Sigma_S/data/`, `SkyQ_Scan/data/`
and `Options_Edge/data/`; your download can live anywhere.

## What is committed

| Path | Contents |
|---|---|
| `published/range_metrics.csv` | Headline metrics for both 1,000-share books |
| `published/equity_curve.csv` | Closed-trade equity curve |
| `published/recent_trades.csv` | Recent-trade sample, not the full ledger |
| `published/futures_gate.csv` | Cross-asset falsification results (including YM) |
| `published/etf_1000sh_comparison.csv` | 1,000-share comparison |
| `published/provenance.json` | Source hashes and generation notes |

The full trade ledgers are produced by the backtest and loaded into PostgreSQL
with `scripts/load_ledgers.py`; they are not duplicated here.

## Stitch a vendor delivery

Many providers deliver date chunks. Put the chunks in a local directory and run:

```bash
python -m scripts.stitch_bars \
  --inputs "/data/SPY_us_stocks_1m_*.csv" \
  --output "data/raw/SPY_1m_stitched.parquet" \
  --symbol SPY
```

The script normalises timestamps to America/New_York, sorts by time, keeps the
last duplicate, drops invalid OHLC rows, and writes no indicator columns.

## Put the bars in SQL

The reporting database (`sql/001_schema.sql`) is intentionally separate from
market bars. The optional table is `market_bars` (`sql/003_market_bars.sql`).

SQLite needs no server:

```bash
python -m scripts.import_market_data \
  --file data/raw/SPY_1m_stitched.parquet \
  --symbol SPY --dataset etf-1m \
  --sqlite data/market/range_market.sqlite --replace
```

PostgreSQL:

```bash
export DATABASE_URL="postgresql+psycopg://postgres:YOUR_PASSWORD@127.0.0.1:5432/skyq_range"
python -m scripts.import_market_data \
  --file data/raw/SPY_1m_stitched.parquet \
  --symbol SPY --dataset etf-1m --replace
```

`--replace` deletes only that `dataset`+`symbol` slice. It does not touch the
reporting trades or the other datasets. A second run without `--replace` will
hit the primary key; use replace for a full reload, or load a new dataset name.

## Run a backtest on your own file

```bash
python -m scripts.backtest_range \
  --symbol SPY --quantity 1000 \
  --data /path/to/your_SPY_1m.parquet \
  --start 2003-09-10 --end 2026-11-04
```

The expected columns are `open, high, low, close, volume` (case-insensitive)
and a timestamp index or a `time`/`timestamp`/`date` column. Extra indicator
columns are ignored. The loader accepts parquet and CSV.

## Run a paper replay on your own file

This exercises the same engine used by the LSE forward runner, including the
next-open fill, stop/target path, costs and 15:55 flat:

```bash
python -m scripts.paper_replay \
  --config configs/runners/spy-1m-paper.json \
  --data /path/to/your_SPY_1m.parquet \
  --start 2026-10-01 --end 2026-11-04
```

Results are written to `reports/paper_replay/`.

## Data hygiene and licensing

- Do not commit vendor data unless your agreement allows redistribution.
- The MIT license covers code and original notes, not third-party data.
- Keep credentials out of data files and runner JSON; use `.env` or a key file.
- Bars are research data, not an execution guarantee. Stops can gap through the
  modelled price and halts/LULD are not represented.

For the LSE live path, no raw archive is required: the bridge downloads its own
warmup after you provide your key. See [`../docs/FORWARD_TESTING.md`](../docs/FORWARD_TESTING.md).
