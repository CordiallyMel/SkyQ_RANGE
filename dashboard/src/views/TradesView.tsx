import { useEffect, useState } from "react";
import { getTrades } from "../api";
import { Icon } from "../components/Icons";
import type { BookKey, TradePage, WindowKey } from "../types";
import { currency, dateTime, signedCurrency, titleCase } from "../lib/format";

const PAGE_SIZE = 25;

export function TradesView({ book, windowKey }: { book: BookKey; windowKey: WindowKey }) {
  const [side, setSide] = useState<"" | "LONG" | "SHORT">("");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<TradePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getTrades(book, windowKey, PAGE_SIZE, page * PAGE_SIZE, side, controller.signal)
      .then((payload) => setData(payload))
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "Could not load trades");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [book, windowKey, side, page]);

  const rows = data?.rows ?? [];
  const wins = rows.filter((row) => row.net_pnl > 0).length;
  const net = rows.reduce((total, row) => total + row.net_pnl, 0);
  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;

  function chooseSide(next: "" | "LONG" | "SHORT") {
    setSide(next);
    setPage(0);
  }

  return (
    <div className="view-stack">
      <section className="page-intro">
        <div>
          <span className="eyebrow">Execution record</span>
          <h1>Trade tape</h1>
          <p>
            The committed database carries a {book} sample of the most recent closed trades.
            Load the full ledger with <code>scripts/load_ledgers.py</code> when you need the
            complete 24-year record in SQL.
          </p>
        </div>
        <div className="intro-stat">
          <span>Sample rows</span>
          <strong>{data ? data.total.toLocaleString() : "—"}</strong>
          <small>newest first</small>
        </div>
      </section>

      <div className="trade-summary">
        <div>
          <span className="eyebrow">Visible net</span>
          <strong className={net >= 0 ? "text-mint" : "text-rose"}>{signedCurrency(net)}</strong>
        </div>
        <div>
          <span className="eyebrow">Visible win rate</span>
          <strong>{rows.length ? `${((wins / rows.length) * 100).toFixed(1)}%` : "—"}</strong>
        </div>
        <div>
          <span className="eyebrow">Page</span>
          <strong>{page + 1} / {totalPages}</strong>
        </div>
        <div className="trade-filter">
          <span className="eyebrow">Side</span>
          <div className="segmented small">
            {(["", "LONG", "SHORT"] as const).map((option) => (
              <button
                key={option || "all"}
                className={side === option ? "active" : ""}
                onClick={() => chooseSide(option)}
              >
                {option || "All"}
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && (
        <div className="error-panel">
          <Icon name="alert" size={18} />
          <div><strong>Could not load the tape</strong><p>{error}</p></div>
        </div>
      )}

      <section className="panel table-panel">
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Exit time</th>
                <th>Side</th>
                <th className="align-right">Entry</th>
                <th className="align-right">Exit</th>
                <th className="align-right">Stop</th>
                <th className="align-right">Target</th>
                <th className="align-right">Bars</th>
                <th>Reason</th>
                <th className="align-right">Net</th>
              </tr>
            </thead>
            <tbody>
              {loading &&
                Array.from({ length: 8 }, (_, index) => (
                  <tr key={`skeleton-${index}`} className="skeleton-row">
                    <td colSpan={9}><div className="skeleton-line" /></td>
                  </tr>
                ))}
              {!loading &&
                rows.map((row) => (
                  <tr key={row.id}>
                    <td className="mono muted">{dateTime(row.exit_time)}</td>
                    <td>
                      <span className={`side-tag side-${row.side.toLowerCase()}`}>{row.side}</span>
                    </td>
                    <td className="align-right mono">{currency(row.entry_price, 2)}</td>
                    <td className="align-right mono">{currency(row.exit_price, 2)}</td>
                    <td className="align-right mono muted">{currency(row.stop_price, 2)}</td>
                    <td className="align-right mono muted">{currency(row.target_price, 2)}</td>
                    <td className="align-right mono muted">{row.bars_held}</td>
                    <td><span className="reason-tag">{titleCase(row.exit_reason)}</span></td>
                    <td className={`align-right mono ${row.net_pnl >= 0 ? "text-mint" : "text-rose"}`}>
                      {signedCurrency(row.net_pnl, 2)}
                    </td>
                  </tr>
                ))}
              {!loading && rows.length === 0 && (
                <tr>
                  <td colSpan={9} className="empty-table">
                    No trades match this filter.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <footer className="table-foot">
          <span>
            Showing {rows.length ? page * PAGE_SIZE + 1 : 0}–
            {Math.min((page + 1) * PAGE_SIZE, data?.total ?? 0)} of {data?.total ?? 0} rows
          </span>
          <div className="pager">
            <button
              className="button button-ghost"
              disabled={page === 0 || loading}
              onClick={() => setPage((current) => Math.max(0, current - 1))}
            >
              Previous
            </button>
            <button
              className="button button-ghost"
              disabled={loading || page + 1 >= totalPages}
              onClick={() => setPage((current) => current + 1)}
            >
              Next
            </button>
          </div>
        </footer>
      </section>

      <div className="callout callout-neutral">
        <Icon name="info" size={18} />
        <p>
          Every row is a closed trade after modeled costs. Times are displayed in New York
          time. The research curve is fixed-size and non-compounded; this tape is a sample,
          not a live order log.
        </p>
      </div>
    </div>
  );
}
