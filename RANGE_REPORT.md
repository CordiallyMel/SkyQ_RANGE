# SkyQ RANGE research report

**Status: current merged study.** This report describes the two 1,000-share ETF
books in this repository. Older 25-share or 2019–2026 notes that appear elsewhere
are historical and are not the basis for the dashboard or the README.

## 1. Scope

SkyQ RANGE is a causal, one-minute, mean-reversion channel scalper. The same
engine runs SPY and QQQ with a separate stop, channel-width floor and time stop.
Both books trade both sides, one position at a time, regular hours only, and are
flat by 15:55 New York.

This is a research record, not a claim of a perpetual edge. The most important
result is not the headline P&L; it is the shape of the distribution and the
cross-asset failure.

## 2. Data and execution

| Item | Value |
|---|---|
| Instrument | SPY, QQQ |
| Timeframe | 1 minute |
| Window | 2003-09-10 through 2026-11-04 |
| Bars | SPY 4,415,353; QQQ 4,075,724 |
| Decision | completed bar `i` |
| Entry | next bar open, one tick slippage |
| Stop / target path | 1-minute highs and lows; stop-first when both are touched |
| Target | resting limit, no slippage |
| Session | regular hours; new entries after 15:55 blocked |
| Position | one at a time; both sides |
| Costs | $0.0035/share, $0.35/order minimum, one tick on entry and market exits |
| Sizing | fixed 1,000 shares; no compounding |

The channel uses a 100-bar normalized close, a 14-bar standard deviation, a
15-bar extreme crossover with at least 10 bars since the prior crossover, and a
10-tick breakout invalidation. A channel known on bar `i` is first usable at
`i+1`. The full construction is in [docs/METHODOLOGY.md](docs/METHODOLOGY.md).

### Presets

| | SPY | QQQ |
|---|---:|---:|
| Stop offset | 100 ticks | 150 ticks |
| Minimum channel width | 0.20% of price | 0.40% of price |
| Maximum hold | 120 bars | 240 bars |
| Entry zone | 20% | 20% |
| Target | 20% of channel | 20% of channel |

## 3. Headline results

| Metric | SPY | QQQ |
|---|---:|---:|
| Trades | 67,271 | 32,356 |
| Wins / losses | 54,207 / 13,064 | 24,408 / 7,948 |
| Win rate | 80.58% | 75.44% |
| Gross | +$2,478,897.85 | +$893,335.10 |
| Costs | -$820,070.00 | -$408,890.00 |
| Net | **+$1,658,827.85** | **+$484,445.10** |
| Expectancy per trade | +$24.66 | +$14.97 |
| Average win / loss | +$175.16 / -$599.83 | +$194.35 / -$535.90 |
| Reward / risk | 0.292 | 0.363 |
| Break-even win rate | 77.4% | 73.4% |
| Win-rate cushion | +3.2 pts | +2.1 pts |
| Profit factor | **1.212** | **1.114** |
| Max closed-trade drawdown | **-$95,845.60** | **-$286,631.20** |
| Sharpe (daily) | 2.53 | 0.92 |
| Sortino (daily) | **5.08** | **1.69** |
| Average planned risk | $1,230.25 | $1,723.91 |
| Positive years | **16 / 24** | **8 / 24** |
| Positive months | 186 / 279 | 106 / 279 |
| Net / month (canonical active-month) | +$5,971.47 | +$1,743.91 |
| Trades / month | 242.2 | 116.5 |
| Exposure | 24.3% | 20.2% |

The published CSV uses a calendar-month denominator (`net / 279`), which produces
the slightly lower +$5,970.24 / +$1,743.55 shown on the dashboard. Both are the
same P&L; only the denominator differs.

The 165.9% SPY and 48.4% QQQ figures sometimes shown beside these results are
**total** return on the fixed $1,000,000 measuring bankroll. They are not annual
returns and not compounded.

### Side attribution

| Book | Side | Trades | Win rate | Net | Profit factor |
|---|---|---:|---:|---:|---:|
| SPY | Long | 33,998 | 81.71% | +$1,005,350.17 | 1.25 |
| SPY | Short | 33,273 | 79.42% | +$653,477.68 | 1.17 |
| QQQ | Long | 16,052 | 76.85% | +$283,666.30 | 1.13 |
| QQQ | Short | 16,304 | 74.04% | +$200,778.80 | 1.10 |

Both sides are positive on both books. They are not additive in a single account
because one position at a time makes them compete for the slot.

### Yearly concentration

SPY's 2025 and 2026 contributions are +$493,777.80 and +$418,120.07, about 55% of
its full-history net. QQQ's 2025 and 2026 contributions are +$234,816.40 and
+$258,543.50, together slightly more than its entire full-history net; every year
from 2003 through 2018 was negative. This is a regime-sensitive volatility
harvester, not a smooth annuity.

The SPY 2019–2022 period was positive overall but weaker than 2023–2026. QQQ's
recovery begins in 2019 and is heavily concentrated in the last two years.
Treat the recent period as the strongest evidence and the earlier period as the
stress test, not the reverse.

## 4. Cross-asset gate

The published ETF geometry was transferred unchanged to ten futures series:
long-only, one contract, 20% channel entry, 0.20% minimum width, 100-tick stop,
20%-of-channel target, 120-bar time stop, 15:55 flat, regular hours, one tick of
slippage and the profile commission.

| Symbol | Contract | Trades | PF | Net | Net / month | Verdict |
|---|---|---:|---:|---:|---:|---|
| MES | Micro E-mini S&P 500 | 9,616 | 0.852 | -$18,498.84 | -$206.49 | negative after costs |
| MNQ | Micro E-mini Nasdaq-100 | 12,293 | 0.891 | -$23,712.32 | -$264.69 | negative after costs |
| M2K | Micro Russell 2000 (RTY proxy) | 13,767 | 0.753 | -$23,788.58 | -$265.54 | negative after costs |
| ZT | 2-Year T-Note | 90 | 0.600 | -$1,448.93 | -$16.17 | negative after costs |
| ES | E-mini S&P 500 | 9,411 | 0.896 | -$125,835.82 | -$1,404.64 | negative after costs |
| NQ | E-mini Nasdaq-100 | 12,203 | 0.937 | -$132,826.36 | -$1,482.67 | negative after costs |
| ZN | 10-Year T-Note | 1,874 | 0.563 | -$42,095.88 | -$469.89 | negative after costs |
| CL | WTI Crude | 20,035 | 0.754 | -$327,594.20 | -$3,656.75 | negative after costs |
| GC | Gold | 6,836 | 0.856 | -$149,074.32 | -$1,664.03 | negative after costs |
| YM | E-mini Dow | 7,313 | 0.916 | -$59,444.56 | -$729.05 | negative after costs |

The high futures win rates do not save the books: the target is too small
relative to the per-contract cost and the wide stop. This is a falsification test,
not a parameter search. A missing file is a data-availability failure, not a pass.
The live runner refuses every futures symbol before opening a broker connection.

## 5. Attribution and one-factor sensitivity

The canonical ledgers are decomposed by side, exit reason, hour, weekday, year,
channel-width quartile and holding time. A separate one-factor sweep changes the
stop, target, entry zone, hold limit, cooldown and side filter while holding the
rest of the baseline fixed. The generated tables are in
[`reports/ATTRIBUTION_AND_SENSITIVITY.md`](reports/ATTRIBUTION_AND_SENSITIVITY.md)
and the machine-readable files are `reports/attribution.csv` and
`reports/sensitivity.csv`.

These are descriptive and stability checks, not a second out-of-sample study.
The selected parameters still live on the same history.

## 6. What changed from the legacy report

The earlier standalone repositories contained a 25-share, 2019–2026 study with a
different both-sides default and an older QQQ conclusion. That work is not
shipped in this checkout and it is **not** the current result. The merged
repository uses:

- a fixed 1,000-share book for both ETFs;
- the full 2003–2026 history plus a common 2007–2026 window;
- a separate QQQ stop (150 ticks), channel floor (0.40%) and time stop (240 bars);
- QQQ presented as positive-but-weaker, not as “does not clear the bar” without
  the full-history context.

Older commands named `scripts.backtest_bocs` and `scripts.research_bocs` are not
part of this standalone repository. Use `scripts.backtest_range` and
`scripts/research_futures`.

## 7. Reproduction

```bash
python -m scripts.backtest_range --symbol SPY --quantity 1000 \
  --data /path/to/SPY_1m_24y.parquet \
  --start 2003-09-10 --end 2026-11-04

python -m scripts.backtest_range --symbol QQQ --quantity 1000 \
  --data /path/to/QQQ_1m_24y.parquet \
  --start 2003-09-10 --end 2026-11-04

python -m scripts.research_futures --data-root /path/to/futures/parquet
```

The exact console reports are in `reports/backtests/`. The portable metrics,
equity and sample trades are in `data/published/`.

## 8. Limitations

- **In-sample selection.** Parameters were selected on the available history;
  the subperiod split is a consistency check, not a clean out-of-sample test.
- **Negative skew.** SPY's average loss is 3.4 average wins; QQQ's is 2.8. The
  drawdown is the risk statistic that matters.
- **Fixed size, closed-trade equity.** No intra-trade mark-to-market, no
  compounding, no account-level margin or financing model.
- **Execution.** A gap can fill worse than the modeled stop. Halts, LULD, borrow
  and locate constraints are not modeled.
- **QQQ quality.** It is positive in the full sample but loses in 16 of 24 years
  and has a 28.7% peak-to-trough drawdown. It is not an equal second book.
- **Futures.** The tested geometry is negative after costs; the gate is not an
  exhaustive search and does not prove no futures variant could work.
- **No live validation.** The IBKR runner is unit-tested and guarded, but no
  Gateway session was available in this workspace.

See [docs/RESULTS.md](docs/RESULTS.md), [docs/METHODOLOGY.md](docs/METHODOLOGY.md)
and [docs/RISK_AND_REGULATION.md](docs/RISK_AND_REGULATION.md).
