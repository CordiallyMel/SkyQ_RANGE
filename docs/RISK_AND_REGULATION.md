# Risk and regulation

Research software, not investment, legal or tax advice. Verify current rules with
your broker and FINRA before trading.

## Pattern-day-trader framework (verified 2026-11-15)

The common shorthand that “PDT was abolished” is close, but imprecise. FINRA
adopted amendments to Rule 4210 in Regulatory Notice 26-10 that **replace the
day-trading margin requirements in their entirety**. The replacement removes:

- the “pattern day trader” definition based on four day trades in five business
  days;
- the $25,000 minimum equity requirement for that designation.

The effective date is **June 4, 2026**. Firms may phase in the amendments until
**October 20, 2027**, so a broker can still apply the old framework during the
transition. Ask your broker which regime is in force for your account.

The replacement is an **intraday margin** requirement, not an absence of margin
rules:

- firms set an intraday margin level for each account;
- an intraday margin deficit must be met promptly;
- the general **$2,000 minimum equity** for a margin account under Rule 4210
  remains;
- maintenance margin still applies, and a broker can liquidate positions.

Sources:

- [FINRA Regulatory Notice 26-10](https://www.finra.org/rules-guidance/notices/26-10)
- [FINRA: Intraday margin requirements](https://www.finra.org/investors/insights/intraday-margin-requirements)
- [FINRA Rule 4210](https://www.finra.org/rules-guidance/rulebooks/finra-rules/4210)

The repository does not model broker margin calls, intraday buying power,
overnight financing or account-level liquidation. A backtest is not a margin
simulation.

## Strategy risks

### Negative skew

The books win most trades because the target is small relative to the stop. A
single stop can erase several targets. The equity curve is smoother than the
trade distribution implies; the losing tail is the risk that matters.

### Fixed size and the dollar illusion

The $1M starting equity and 1,000-share size make the dollars comparable across
books. They do not mean the strategy compounds, and the percentage of the
starting bankroll is not an annual return. At a smaller account, the same share
count has a different risk profile; at a larger one, market impact and fill
quality change.

### Execution assumptions

- next-bar-open entry;
- one tick of entry and market-exit slippage;
- stop-first when a bar touches both stop and target;
- no overnight positions;
- no borrow, locate or short-sale constraint modeled;
- no halt, LULD or exchange outage modeled.

A real fill can be worse. A gap through the stop can lose more than the modeled
risk per trade.

### Regime dependence

The report shows a strong recent period and a weak 2019–2022 period. The strategy
is not a perpetual money machine; it is a conditional mean-reversion process.
The futures gate shows the geometry is not universal.

### Data and survivorship

SPY and QQQ are liquid, long-lived ETFs, which reduces survivorship bias relative
to single stocks. The sample still depends on the vendor's split/dividend
adjustment and timestamp conventions. Read `data/published/provenance.json` and
the report for the exact source.

## Operational risks

- A local API with control enabled is a local privileged surface. Keep it on
  loopback and disabled by default.
- A broker adapter can place orders when `--execute` is used. Paper first.
- A vendor key can be rate-limited, revoked or billed per request. Quote before
  downloading.
- A database password in `.env` is still a secret. Do not commit it or paste it
  into an issue.

## What this repository does not claim

- It does not claim the strategy will work in the future.
- It does not claim futures are viable; the tested set is negative after costs.
- It does not claim QQQ is equal to SPY.
- It does not claim PDT is gone for every broker during the transition.
- It does not provide legal, tax or investment advice.
