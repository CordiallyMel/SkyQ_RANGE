# Offline paper-engine replay

This is a **historical replay**, not a live LSE session. It exists to prove that
the same paper engine used by `scripts/run_lse_runner.py` consumes completed
one-minute bars and produces a journal.

| Item | Value |
|---|---|
| Runner config | `configs/runners/spy-1m-paper.json` |
| Data | owner's `SPY_1m_24y.parquet` |
| Slice | 2026-10-01 through 2026-11-04 |
| Bars | 9,750 |
| Trades | 282 |
| Net after costs | +$27,769.40 |
| Win rate | 80.50% |
| Profit factor | 1.322 |
| Max closed-trade drawdown | -$15,555.00 |

Files:

- `spy-1m-paper.csv` — closed-trade journal
- `spy-1m-paper.json` — run summary and metrics

Reproduce with your own file:

```bash
python -m scripts.paper_replay \
  --config configs/runners/spy-1m-paper.json \
  --data /path/to/SPY_1m.parquet \
  --start 2026-10-01 --end 2026-11-04 \
  --output-dir reports/paper_replay
```

The replay uses only 09:30–15:59 ET bars, matching the live bridge's RTH-only
bar builder. It is not evidence of live fill quality, and it is not the
canonical 67,271-trade result.
