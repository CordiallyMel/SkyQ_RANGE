# Methodology

## Inputs

The engine consumes 1-minute OHLCV bars. It never reads the parquet indicator
columns (ADX, Bollinger, RSI, VWAP); everything is derived from OHLCV. The
optional macro-event calendar is not enabled in the default books.

Bars are timestamped at their open. A bar is only usable after it closes. The
decision bar `i` is complete; the earliest entry is the open of `i+1`.

## Channel construction

1. Normalize price over a 100-bar window:
   `normalized = (close - lowest(low, 100)) / (highest(high, 100) - lowest(low, 100))`.
2. Take a 14-bar standard deviation of that normalized series.
3. Detect a volatility crossover with a 15-bar highest/lowest-bars window and a
   minimum of 10 bars since the previous crossover.
4. The channel top and bottom are the highest high and lowest low over that
   duration.
5. A close more than 10 ticks outside the channel invalidates it.

A channel that becomes known on bar `i` cannot be traded before `i+1`.

## Entry and exit

- **Long entry:** lower 20% of the channel.
- **Short entry:** upper 20% of the channel.
- **Target:** 20% of the channel range from the entry reference.
- **Stop:** beyond the channel edge by the preset's tick offset.
- **Time stop:** 120 bars for SPY, 240 for QQQ.
- **Session:** regular hours; flat by 15:55 New York.
- **Position limit:** one at a time. Multiple channels may exist, but they
  compete for the single position.

The backtest fills at the next bar open with one tick of entry slippage. The
target is a resting limit and is not slipped. Stop and time exits are market
orders and are slipped. If a bar touches both the stop and the target, the
stop is assumed first.

## Presets

| | SPY | QQQ |
|---|---:|---:|
| Stop offset | 100 ticks | 150 ticks |
| Minimum channel width | 0.20% of price | 0.40% of price |
| Maximum hold | 120 bars | 240 bars |
| Entry zone | 20% | 20% |
| Target | 20% of channel | 20% of channel |
| Sizing | 1,000 shares | 1,000 shares |

The relative width floor is deliberate. A fixed dollar floor becomes looser as
an ETF's price rises; 0.20% keeps the same selectivity across the sample.

## Costs

| Book | Commission model | Slippage |
|---|---|---|
| SPY / QQQ | $0.0035/share, $0.35 minimum per order | one tick on entry and market exits |

The published reports show gross, costs and net separately. The dashboard's
headline is net. Costs are modeled assumptions, not a broker quote; a real
execution venue can be worse.

## What the $1M curve means

The account starts at $1,000,000 and trades a **fixed 1,000 shares**. Profits are
retained as cash but are not reinvested. The resulting curve is useful for
comparing the two books and for seeing drawdown, but it is not a compounded
account, not a CAGR and not an annual return. A percentage of the starting
notional is shown only as a scale reference.

## Robustness and limitations

- The edge is regime-dependent. The report documents a stronger 2023–2026
  period and a much weaker 2019–2022 period.
- The high win rate is paired with negative skew. A small number of stop-outs
  can dominate a run of targets.
- Closed-trade equity does not show intra-trade mark-to-market or overnight gap
  risk. The strategy is flat by 15:55, but fills can gap.
- The futures test is a transfer test, not an exhaustive search. It shows the
  published ETF geometry fails after costs; it does not prove no futures variant
  could work.
- QQQ is supported but materially weaker than SPY. It is not marketed as an equal
  book.

See [RESULTS.md](RESULTS.md) and the canonical `RANGE_REPORT.md`.
