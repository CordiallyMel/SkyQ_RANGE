-- Convenience views for analysts and the dashboard SQL examples.

CREATE OR REPLACE VIEW v_range_metrics AS
SELECT
    r.symbol,
    i.name,
    r."window",
    r.quantity,
    r.preset,
    m.trades,
    m.win_rate,
    m.profit_factor,
    m.net,
    m.return_pct,
    m.max_drawdown,
    m.max_drawdown_pct,
    m.sharpe,
    m.net_per_month,
    m.positive_months,
    m.months,
    m.positive_years,
    m.years,
    m.costs
FROM backtest_runs r
JOIN instruments i ON i.symbol = r.symbol
JOIN backtest_metrics m ON m.run_id = r.id
WHERE r.strategy = 'SkyQ RANGE';

CREATE OR REPLACE VIEW v_trade_attribution AS
SELECT
    r.symbol,
    r."window",
    t.side,
    t.exit_reason,
    count(*) AS trades,
    round(avg(t.win_rate_proxy)::numeric, 4) AS win_rate,
    round(sum(t.net_pnl)::numeric, 2) AS net,
    round(avg(t.net_pnl)::numeric, 2) AS avg_trade
FROM backtest_runs r
JOIN (
    SELECT *, CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END AS win_rate_proxy
    FROM trades
) t ON t.run_id = r.id
GROUP BY r.symbol, r."window", t.side, t.exit_reason;
