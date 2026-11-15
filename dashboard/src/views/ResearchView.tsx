import { GateBars } from "../components/Charts";
import { Icon } from "../components/Icons";
import type { Book, FuturesRow } from "../types";
import {
  currency,
  number,
  percentFraction,
  signedCurrency,
} from "../lib/format";

export function ResearchView({
  futures,
  books,
  onOpenTrades,
}: {
  futures: FuturesRow[];
  books: Book[];
  onOpenTrades: () => void;
}) {
  const canonical = books.filter((book) => book.window === "2003-2026");

  return (
    <div className="view-stack">
      <section className="page-intro research-intro">
        <div>
          <span className="eyebrow">Falsification record</span>
          <h1>The gate that says no.</h1>
          <p>
            A strategy is only interesting if it survives outside its home tape. RANGE was
            run unchanged on the futures we have, with contract-specific tick values and
            costs. Every series is negative after costs. That is the result, not a footnote.
          </p>
        </div>
        <div className="stamp-card">
          <span className="eyebrow">Verdict</span>
          <strong>ETF only</strong>
          <span>No futures promotion</span>
        </div>
      </section>

      <div className="research-grid">
        <section className="panel gate-panel">
          <header className="panel-head">
            <div>
              <span className="eyebrow">Cross-asset gate</span>
              <h2>Net per month, after costs</h2>
            </div>
            <span className="badge badge-rose">{futures.length} / {futures.length} negative</span>
          </header>
          <GateBars rows={futures} />
          <p className="panel-note">
            The bars are monthly net P&L. The same channel geometry that pays on SPY and QQQ
            gives the edge back to commissions and slippage on index, energy, metals and
            rates futures.
          </p>
        </section>

        <section className="panel method-panel">
          <header className="panel-head compact">
            <div>
              <span className="eyebrow">Method</span>
              <h2>What was held constant</h2>
            </div>
            <Icon name="flask" size={20} />
          </header>
          <ul className="method-list">
            <li><span>Entry</span><p>Lower 20% of the normalized channel; close back through the edge.</p></li>
            <li><span>Exit</span><p>20% channel target, stop beyond the edge, or time stop.</p></li>
            <li><span>Session</span><p>Regular hours, flat by 15:55 New York.</p></li>
            <li><span>Costs</span><p>Per-contract commission plus one tick of slippage on market fills.</p></li>
            <li><span>Data</span><p>Databento 1-minute continuous futures for the cross-asset test.</p></li>
          </ul>
        </section>
      </div>

      <section className="panel table-panel">
        <header className="panel-head compact">
          <div>
            <span className="eyebrow">Full gate</span>
            <h2>Every tested series</h2>
          </div>
          <span className="badge">after costs</span>
        </header>
        <div className="table-scroll">
          <table className="data-table gate-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Contract</th>
                <th className="align-right">Bars</th>
                <th className="align-right">Trades</th>
                <th className="align-right">Win rate</th>
                <th className="align-right">PF</th>
                <th className="align-right">Net</th>
                <th className="align-right">Net / month</th>
                <th className="align-right">Positive years</th>
                <th>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {futures.map((row) => (
                <tr key={row.symbol}>
                  <td><strong>{row.symbol}</strong></td>
                  <td className="muted">{row.description}</td>
                  <td className="align-right mono muted">{number(row.bars)}</td>
                  <td className="align-right mono">{number(row.trades)}</td>
                  <td className="align-right mono muted">{percentFraction(row.win_rate)}</td>
                  <td className="align-right mono muted">{row.profit_factor.toFixed(3)}</td>
                  <td className="align-right mono text-rose">{currency(row.net)}</td>
                  <td className="align-right mono text-rose">{currency(row.net_per_month)}</td>
                  <td className="align-right mono muted">{row.positive_years}/{row.years}</td>
                  <td><span className="verdict-tag">{row.verdict}</span></td>
                </tr>
              ))}
              {futures.length === 0 && (
                <tr><td colSpan={10} className="empty-table">No futures gate rows loaded.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <div className="callout callout-danger">
        <Icon name="alert" size={20} />
        <div>
          <strong>Do not trade this on futures.</strong>
          <p>
            M2K uses RTY data as a proxy; ZT has only a small sample and is a specialized
            rates contract. Those caveats do not rescue the result: the sign is negative
            across every symbol, and no futures book is enabled by <code>src/main.py</code>.
          </p>
        </div>
      </div>

      <section className="panel books-evidence">
        <header className="panel-head compact">
          <div>
            <span className="eyebrow">Where it does work</span>
            <h2>The two ETF books, full history</h2>
          </div>
          <button className="text-button" onClick={onOpenTrades}>
            Open trade tape <Icon name="arrowUpRight" size={14} />
          </button>
        </header>
        <div className="evidence-cards">
          {canonical.map((book) => (
            <div className="evidence-card" key={book.symbol}>
              <div className="evidence-card-head">
                <strong>{book.symbol}</strong>
                <span>{book.quantity.toLocaleString()} shares</span>
              </div>
              <div className="evidence-net">{signedCurrency(book.metrics.net)}</div>
              <div className="evidence-meta">
                <span>PF {book.metrics.profit_factor.toFixed(3)}</span>
                <span>{number(book.metrics.trades)} trades</span>
                <span>{percentFraction(book.metrics.win_rate)} win</span>
                <span>DD {currency(book.metrics.max_drawdown)}</span>
              </div>
            </div>
          ))}
        </div>
        <p className="panel-note">
          SPY is the full-history reference. QQQ is supported by the same engine but is a
          weaker, higher-drawdown book: lower profit factor and only 8 of 24 positive
          years. Neither result is a promise about the next 24 years.
        </p>
      </section>

      <section className="research-notes">
        <div>
          <span className="eyebrow">Why the failure matters</span>
          <h3>Different microstructure, same geometry</h3>
          <p>
            Futures have different tick economics, overnight gaps and volatility regimes.
            A one-minute ETF scalp should not be expected to transplant cleanly. The gate
            prevents us from pretending it does.
          </p>
        </div>
        <div>
          <span className="eyebrow">What survived</span>
          <h3>Costs are the first filter</h3>
          <p>
            The ETF books remain positive after commissions and slippage. Every intraday
            system in this repository is judged on net, not gross; the gross number is never
            the headline.
          </p>
        </div>
        <div>
          <span className="eyebrow">What to test next</span>
          <h3>Regime, not more symbols</h3>
          <p>
            The interesting question is when the channel filter stops working, not whether
            another symbol can be forced to fit. That is the research direction documented
            in the repository report.
          </p>
        </div>
      </section>
    </div>
  );
}
