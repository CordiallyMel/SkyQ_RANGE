# Attribution and sensitivity notes

The generated tables are in:

- [`../reports/ATTRIBUTION_AND_SENSITIVITY.md`](../reports/ATTRIBUTION_AND_SENSITIVITY.md)
- `reports/attribution.csv` — bucket-level decomposition for SPY and QQQ
- `reports/sensitivity.csv` — one-factor sweep for both 1,000-share books
- `reports/attribution_SPY.md`, `reports/attribution_QQQ.md` — readable copies
- per-symbol/per-dimension CSVs under `reports/`

Reproduce with the full one-minute files outside Git:

```bash
python -m scripts.research_attribution \
  --data-root /path/to/Data_For_Backtests \
  --out reports
```

The baseline run reproduces the canonical metrics exactly. The sweep changes one
knob at a time and does not search a grid.

## SPY: where the P&L came from

- Both sides contribute: LONG +$1,005,350 (60.6% of net), SHORT +$653,478
  (39.4%). The long side also has the better PF (1.254 vs 1.169).
- TARGET exits are +$9.37M gross; STOP exits are -$5.68M, FLAT exits -$0.94M and
  MAX_HOLD exits -$1.10M. The strategy is a high-hit-rate, negative-tail book:
  the target bucket is the engine, not a rounding error.
- The widest channel quartile contributes +$1.34M, about 81% of net. The
  narrowest quartile is slightly negative after costs. This is consistent with
  the preset's relative channel-width floor.
- 2025 (+$493,778) and 2026 (+$418,120) together are about 55% of the full-sample
  net. That concentration is a warning, not a victory lap.
- Friday/Tuesday are the strongest weekdays; Monday is the weakest. Do not turn
  that into a day filter without a pre-specified test.

## SPY: one-factor sensitivity

| Change | Net | Delta vs baseline | Read |
|---|---:|---:|---|
| Stop 50t | +$1,212,324 | -$446,504 | More trades, materially lower net and higher DD |
| Stop 100t (baseline) | +$1,658,828 | — | Canonical |
| Stop 150t | +$1,791,514 | +$132,687 | Higher net; DD is similar, not lower |
| Target 10% | +$1,293,356 | -$365,472 | More trades, much higher DD |
| Target 20% (baseline) | +$1,658,828 | — | Canonical |
| Target 30% | +$1,426,709 | -$232,119 | Fewer trades, lower net, better DD |
| Entry zone 10% | +$1,473,868 | -$184,960 | Shallower entries are worse |
| Entry zone 30% | +$1,776,178 | +$117,350 | Slightly better net in-sample |
| Cooldown 4 bars | +$1,300,345 | -$358,483 | Lower net and lower DD |
| Cooldown 8 bars | +$1,271,536 | -$387,292 | Lower net and lower DD |
| Long only | +$1,146,690 | -$512,138 | Higher PF, lower absolute net |
| Short only | +$1,078,043 | -$580,785 | Still positive, lower net |

The stop-150 and entry-zone-0.3 results are in-sample temptations. They are not
the published book because changing them after seeing the full history would be
selection, not validation.

## QQQ: the weaker book

QQQ's decomposition is similar in direction but thinner:

- LONG +$283,666 (58.6% of net), SHORT +$200,779 (41.4%).
- TARGET +$4.70M; STOP -$3.01M; FLAT -$0.70M; MAX_HOLD -$0.50M.
- The full-sample PF is 1.114, the max drawdown is -$286,631, and only 8 of 24
  years are positive. A positive full-sample total does not make this an equal
  second book.

The QQQ sweep is in `reports/sensitivity.csv`. Treat small improvements as
fragile: the book has less margin for parameter error than SPY.

## What this does and does not establish

**Does:** show where the closed-trade P&L is concentrated, whether both sides
matter, and how much one deployment knob moves the result.

**Does not:** provide a clean out-of-sample test, model intra-trade drawdown,
prove a bucket will persist, or justify adding a new filter after the fact. The
canonical result is still a fixed-size, closed-trade study on the same history
used to choose the presets.

For the exact numbers by year, hour, weekday, width quartile and holding time,
read the per-symbol CSVs. For the audit trail, use `data/published/provenance.json`
and the canonical reports in `reports/backtests/`.
