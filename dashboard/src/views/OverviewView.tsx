import { EquityChart } from "../components/EquityChart";
import { MonthlyBars } from "../components/Charts";
import { Icon } from "../components/Icons";
import { MetricCard } from "../components/MetricCard";
import type {
  BookKey,
  ControlStatus,
  FuturesRow,
  Overview,
  WindowKey,
} from "../types";
import {
  compactCurrency,
  currency,
  number,
  percentFraction,
  percentValue,
  shortDate,
  signedCurrency,
} from "../lib/format";

const PRESETS: Record<BookKey, { stop: string; floor: string; hold: string; label: string }> = {
  SPY: { stop: "100 ticks", floor: "0.20%", hold: "120 bars", label: "Full-history reference" },
  QQQ: { stop: "150 ticks", floor: "0.40%", hold: "240 bars", label: "Weaker second book" },
};

export function OverviewView({
  overview,
  futures,
  book,
  windowKey,
  control,
  loading,
  error,
  onBook,
  onWindow,
  onOpenResearch,
  onOpenSystem,
  onRetry,
}: {
  overview: Overview | null;
  futures: FuturesRow[];
  book: BookKey;
  windowKey: WindowKey;
  control: ControlStatus | null;
  loading: boolean;
  error: string | null;
  onBook: (book: BookKey) => void;
  onWindow: (window: WindowKey) => void;
  onOpenResearch: () => void;
  onOpenSystem: () => void;
  onRetry: () => void;
}) {
  const preset = PRESETS[book];
  const metrics = overview?.metrics;

  return (
    <div className="view-stack">
      <section className="hero">
        <div className="hero-copy">
          <div className="hero-kicker">
            <span className="pulse" />
            Systematic ETF desk / research build 1.0
          </div>
          <h1>
            Two books.
            <br />
            <span>One structural engine.</span>
          </h1>
          <p>
            RANGE harvests the most repetitive part of the one-minute session: buy the lower
            edge of a quiet channel, sell the upper edge, and stay flat when the channel is
            too wide or too thin. The same code runs both ETF books with a separate stop and
            time horizon for each.
          </p>
          <div className="hero-actions">
            <button className="button button-primary" onClick={onOpenResearch}>
              <Icon name="flask" size={16} />
              Cross-asset evidence
            </button>
            <button className="button button-ghost" onClick={onOpenSystem}>
              <Icon name="system" size={16} />
              System status
            </button>
          </div>
        </div>
        <div className="hero-side">
          <div className="hero-side-top">
            <span className="eyebrow">Current book</span>
            <span className="badge badge-mint">ETF / paper first</span>
          </div>
          <div className="hero-book-row">
            <div>
              <span className="hero-symbol">{book}</span>
              <span className="hero-book-name">
                {book === "SPY" ? "S&P 500 trust" : "Nasdaq-100 trust"}
              </span>
            </div>
            <div className="hero-book-net">
              {metrics ? signedCurrency(metrics.net) : "—"}
              <small>closed-trade net</small>
            </div>
          </div>
          <div className="hero-side-rule" />
          <div className="hero-facts">
            <div>
              <span>Preset</span>
              <strong>{preset.stop} / {preset.floor}</strong>
            </div>
            <div>
              <span>Window</span>
              <strong>{windowKey}</strong>
            </div>
            <div>
              <span>Monitor</span>
              <strong className={control?.running ? "text-mint" : "text-muted"}>
                {control?.running ? `running · ${control.symbol}` : "paper monitor idle"}
              </strong>
            </div>
          </div>
          <div className="hero-book-picker">
            {(["SPY", "QQQ"] as const).map((symbol) => (
              <button
                key={symbol}
                className={`book-chip ${book === symbol ? "active" : ""}`}
                onClick={() => onBook(symbol)}
              >
                <span>{symbol}</span>
                <small>{PRESETS[symbol].label}</small>
              </button>
            ))}
          </div>
        </div>
      </section>

      <div className="toolbar">
        <div className="toolbar-group">
          <span className="eyebrow">Window</span>
          <div className="segmented small">
            <button
              className={windowKey === "2003-2026" ? "active" : ""}
              onClick={() => onWindow("2003-2026")}
            >
              2003–2026
            </button>
            <button
              className={windowKey === "2007-2026" ? "active" : ""}
              onClick={() => onWindow("2007-2026")}
            >
              2007–2026
            </button>
          </div>
        </div>
        <div className="toolbar-meta">
          <Icon name="clock" size={15} />
          <span>
            {overview
              ? `${shortDate(overview.start)} → ${shortDate(overview.end)} · 1-min RTH`
              : "Loading research window"}
          </span>
          {loading && <span className="tiny-loader" />}
        </div>
      </div>

      {error && (
        <div className="error-panel">
          <Icon name="alert" size={18} />
          <div>
            <strong>Read model unavailable</strong>
            <p>{error}</p>
          </div>
          <button className="button button-ghost" onClick={onRetry}>
            <Icon name="refresh" size={15} />
            Retry
          </button>
        </div>
      )}

      {!metrics && !error && (
        <div className="metric-grid">
          {Array.from({ length: 6 }, (_, index) => (
            <div className="metric-card skeleton" key={index}>
              <div className="skeleton-line short" />
              <div className="skeleton-line tall" />
              <div className="skeleton-line" />
            </div>
          ))}
        </div>
      )}

      {metrics && (
        <div className="metric-grid">
          <MetricCard
            label="Net P&L"
            value={signedCurrency(metrics.net)}
            detail={`${percentValue(metrics.return_pct)} on a $1M fixed bankroll`}
            tone="positive"
            icon={<Icon name="activity" size={17} />}
          />
          <MetricCard
            label="Profit factor"
            value={metrics.profit_factor.toFixed(3)}
            detail={`${number(metrics.trades)} closed trades · ${number(metrics.trades / metrics.months, 1)}/mo`}
            tone="neutral"
            icon={<Icon name="layers" size={17} />}
          />
          <MetricCard
            label="Win rate"
            value={percentFraction(metrics.win_rate)}
            detail={`${metrics.positive_months}/${metrics.months} positive months`}
            tone="neutral"
            icon={<Icon name="check" size={17} />}
          />
          <MetricCard
            label="Max drawdown"
            value={currency(metrics.max_drawdown)}
            detail={`${percentValue(metrics.max_drawdown_pct)} peak-to-trough`}
            tone="negative"
            icon={<Icon name="shield" size={17} />}
          />
          <MetricCard
            label="Sharpe"
            value={metrics.sharpe.toFixed(2)}
            detail={`daily · ${metrics.positive_years}/${metrics.years} positive years`}
            tone={metrics.sharpe >= 1 ? "positive" : "amber"}
            icon={<Icon name="activity" size={17} />}
          />
          <MetricCard
            label="Net / month"
            value={signedCurrency(metrics.net_per_month)}
            detail={`${currency(metrics.costs)} modeled costs`}
            tone="positive"
            icon={<Icon name="clock" size={17} />}
          />
        </div>
      )}

      <div className="overview-grid">
        <div className="main-column">
          <section className="panel chart-panel">
            <header className="panel-head">
              <div>
                <span className="eyebrow">Equity curve</span>
                <h2>Fixed size, no compounding</h2>
              </div>
              <div className="panel-legend">
                <span className="legend-line" />
                <span>equity</span>
                <span className="legend-dash" />
                <span>starting bankroll</span>
              </div>
            </header>
            {overview ? (
              <EquityChart
                points={overview.equity}
                startingEquity={overview.starting_equity}
              />
            ) : (
              <div className="chart-empty">Loading closed-trade curve…</div>
            )}
            <footer className="panel-foot">
              <div>
                <span>Start</span>
                <strong>{overview ? compactCurrency(overview.starting_equity) : "—"}</strong>
              </div>
              <div>
                <span>Latest equity</span>
                <strong>
                  {overview && overview.equity.length > 0
                    ? compactCurrency(overview.equity[overview.equity.length - 1].equity)
                    : "—"}
                </strong>
              </div>
              <div>
                <span>Peak-to-trough</span>
                <strong className="text-rose">
                  {metrics ? currency(metrics.max_drawdown) : "—"}
                </strong>
              </div>
              <div>
                <span>Model</span>
                <strong>fixed 1,000 sh</strong>
              </div>
            </footer>
          </section>

          <div className="lower-grid">
            <section className="panel">
              <header className="panel-head compact">
                <div>
                  <span className="eyebrow">Month by month</span>
                  <h2>Consistency, not a straight line</h2>
                </div>
                <span className="badge">{metrics ? `${metrics.positive_months}/${metrics.months}` : "—"} up</span>
              </header>
              {overview ? <MonthlyBars data={overview.monthly} /> : <div className="chart-empty">Loading…</div>}
            </section>

            <section className="panel evidence-panel">
              <header className="panel-head compact">
                <div>
                  <span className="eyebrow">Cross-asset gate</span>
                  <h2>Same geometry, different tape</h2>
                </div>
                <button className="text-button" onClick={onOpenResearch}>
                  Full gate <Icon name="arrowUpRight" size={14} />
                </button>
              </header>
              <div className="gate-mini">
                {futures.length === 0 && <div className="empty-note">No futures evidence loaded.</div>}
                {futures.map((row) => (
                  <div className="gate-mini-row" key={row.symbol}>
                    <span className="gate-mini-symbol">{row.symbol}</span>
                    <span className="gate-mini-name">{row.description}</span>
                    <span className="gate-mini-value">{currency(row.net_per_month)}/mo</span>
                    <span className="status-dot status-dot-red" />
                  </div>
                ))}
              </div>
              <p className="panel-note">
                RANGE is an ETF book. The identical entry/exit logic loses money after costs on
                every futures series tested, including MES, MNQ, M2K and ZT.
              </p>
            </section>
          </div>
        </div>

        <aside className="side-column">
          <section className="panel preset-panel">
            <header className="panel-head compact">
              <div>
                <span className="eyebrow">Deployment envelope</span>
                <h2>{book} preset</h2>
              </div>
              <span className="badge badge-mint">ETF only</span>
            </header>
            <div className="level-list">
              <div><span>Stop offset</span><strong>{preset.stop}</strong></div>
              <div><span>Channel floor</span><strong>{preset.floor}</strong></div>
              <div><span>Max hold</span><strong>{preset.hold}</strong></div>
              <div><span>Entry zone</span><strong>bottom 20%</strong></div>
              <div><span>Session</span><strong>RTH · flat 15:55</strong></div>
              <div><span>Sizing</span><strong>1,000 shares</strong></div>
            </div>
            <p className="panel-note">
              {preset.label}. The stop, floor and hold are the only parameters that differ
              between the two books.
            </p>
          </section>

          <section className="panel deploy-panel">
            <header className="panel-head compact">
              <div>
                <span className="eyebrow">Operational posture</span>
                <h2>Paper before principal</h2>
              </div>
              <span className="badge badge-amber">no live order API</span>
            </header>
            <div className="deploy-list">
              <div>
                <span>Research</span>
                <strong className="text-mint">Ready</strong>
              </div>
              <div>
                <span>Paper monitor</span>
                <strong>{control?.running ? "Running" : "Idle / opt-in"}</strong>
              </div>
              <div>
                <span>Live execution</span>
                <strong className="text-muted">Not exposed</strong>
              </div>
              <div>
                <span>Risk guards</span>
                <strong>2% risk · 3% daily stop</strong>
              </div>
              <div>
                <span>Broker bridge</span>
                <strong>IBKR paper · 4002</strong>
              </div>
            </div>
            <button className="button button-outline full-width" onClick={onOpenSystem}>
              Inspect system & brokers
              <Icon name="arrowUpRight" size={15} />
            </button>
          </section>

          <section className="panel quote-panel">
            <span className="quote-mark">“</span>
            <p>
              The edge is not a prediction. It is a repeatable response to a quiet,
              range-bound tape — and it disappears when the tape changes character.
            </p>
            <span className="eyebrow">Research note / RANGE</span>
          </section>
        </aside>
      </div>

      <section className="about-strip">
        <div>
          <span className="eyebrow">01 / Channel</span>
          <h3>Quiet tape, defined edges</h3>
          <p>
            A normalized range detector finds compression. The lower 20% of the channel is
            the entry zone; a close outside the channel invalidates it.
          </p>
        </div>
        <div>
          <span className="eyebrow">02 / Exit</span>
          <h3>Target inside, stop outside</h3>
          <p>
            A 20%-of-channel target keeps the hit rate high. The stop sits beyond the channel
            edge so ordinary noise does not end the trade.
          </p>
        </div>
        <div>
          <span className="eyebrow">03 / Truth</span>
          <h3>Costs and counterexamples</h3>
          <p>
            Every result here is after modeled commissions and slippage. The futures section
            exists because a strategy that only works on one tape is not a strategy.
          </p>
        </div>
      </section>
    </div>
  );
}
