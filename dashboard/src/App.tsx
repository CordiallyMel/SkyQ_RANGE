import { useEffect, useState } from "react";
import { control, getBooks, getFutures, getOverview, getSystem } from "./api";
import { Icon, type IconName } from "./components/Icons";
import { ForwardView } from "./views/ForwardView";
import { OverviewView } from "./views/OverviewView";
import { ResearchView } from "./views/ResearchView";
import { SystemView } from "./views/SystemView";
import { TradesView } from "./views/TradesView";
import type {
  Book,
  BookKey,
  FuturesRow,
  Overview,
  SystemInfo,
  ViewKey,
  WindowKey,
} from "./types";

const NAV: { key: ViewKey; label: string; caption: string; icon: IconName }[] = [
  { key: "overview", label: "Overview", caption: "Books & risk", icon: "dashboard" },
  { key: "trades", label: "Trade tape", caption: "Closed trades", icon: "trades" },
  { key: "research", label: "Research", caption: "Cross-asset gate", icon: "research" },
  { key: "forward", label: "Forward", caption: "Runner lab", icon: "flask" },
  { key: "system", label: "System", caption: "Data & control", icon: "system" },
];

const TITLES: Record<ViewKey, string> = {
  overview: "Overview",
  trades: "Trade tape",
  research: "Research gate",
  forward: "Forward runner lab",
  system: "System & control",
};

export default function App() {
  const [view, setView] = useState<ViewKey>("overview");
  const [book, setBook] = useState<BookKey>("SPY");
  const [windowKey, setWindowKey] = useState<WindowKey>("2003-2026");
  const [overview, setOverview] = useState<Overview | null>(null);
  const [books, setBooks] = useState<Book[]>([]);
  const [futures, setFutures] = useState<FuturesRow[]>([]);
  const [system, setSystem] = useState<SystemInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [metaError, setMetaError] = useState<string | null>(null);
  const [refreshToken, setRefreshToken] = useState(0);
  const [controlBusy, setControlBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([getBooks(controller.signal), getFutures(controller.signal), getSystem(controller.signal)])
      .then(([bookRows, futureRows, systemInfo]) => {
        setBooks(bookRows);
        setFutures(futureRows);
        setSystem(systemInfo);
        setMetaError(null);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setMetaError(reason instanceof Error ? reason.message : "Could not reach the API");
      });
    return () => controller.abort();
  }, [refreshToken]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getOverview(book, windowKey, controller.signal)
      .then((payload) => setOverview(payload))
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setOverview(null);
        setError(reason instanceof Error ? reason.message : "Could not load the book");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [book, windowKey, refreshToken]);

  async function handleControl(action: "start" | "stop") {
    if (
      action === "start" &&
      !window.confirm(
        `Start the fixed paper monitor for ${book}? This launches src.main without --execute.`,
      )
    ) {
      return;
    }
    setControlBusy(true);
    setControlError(null);
    try {
      await control(action, book);
      const fresh = await getSystem();
      setSystem(fresh);
    } catch (reason) {
      setControlError(reason instanceof Error ? reason.message : "Control action failed");
    } finally {
      setControlBusy(false);
    }
  }

  const connected = Boolean(system) && !metaError;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">R</div>
          <div className="brand-copy">
            <strong>SkyQ</strong>
            <span>RANGE</span>
          </div>
        </div>

        <div className="sidebar-section-label">Research workspace</div>
        <nav className="nav" aria-label="Primary">
          {NAV.map((item) => (
            <button
              key={item.key}
              className={`nav-item ${view === item.key ? "active" : ""}`}
              aria-label={item.label}
              aria-current={view === item.key ? "page" : undefined}
              onClick={() => setView(item.key)}
            >
              <span className="nav-icon"><Icon name={item.icon} size={18} /></span>
              <span className="nav-label">
                {item.label}
                <small>{item.caption}</small>
              </span>
              {view === item.key && <span className="nav-active-mark" />}
            </button>
          ))}
        </nav>

        <div className="sidebar-spacer" />

        <div className="sidebar-status">
          <div className="sidebar-status-head">
            <span className={`status-dot ${connected ? "status-dot-green" : "status-dot-amber"}`} />
            <span>{connected ? "SQL read model online" : "API connecting"}</span>
          </div>
          <span className="sidebar-status-detail">
            {system ? `${system.database.dialect} · ${system.database.trades.toLocaleString()} trades in SQL` : "Waiting for FastAPI"}
          </span>
        </div>

        <div className="sidebar-footer">
          <span>v1.0.0 · MIT</span>
          <span>Research only. No financial advice.</span>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <div className="topbar-title">
            <span className="crumb">RANGE</span>
            <span className="crumb-sep">/</span>
            <strong>{TITLES[view]}</strong>
          </div>
          <div className="topbar-actions">
            <span className="connection">
              <span className={`status-dot ${connected ? "status-dot-green" : "status-dot-amber"}`} />
              {metaError ? "API issue" : connected ? "API connected" : "connecting"}
            </span>
            <span className="paper-pill">PAPER FIRST</span>
            <button
              className="icon-button"
              title="Refresh all data"
              aria-label="Refresh all data"
              onClick={() => setRefreshToken((current) => current + 1)}
            >
              <Icon name="refresh" size={17} className={loading ? "spin" : undefined} />
            </button>
          </div>
        </header>

        {metaError && (
          <div className="top-error">
            <Icon name="alert" size={16} />
            <span>{metaError}</span>
            <button className="text-button" onClick={() => setRefreshToken((current) => current + 1)}>
              Retry
            </button>
          </div>
        )}

        <main className="content">
          {view === "overview" && (
            <OverviewView
              overview={overview}
              futures={futures}
              book={book}
              windowKey={windowKey}
              control={system?.control ?? null}
              loading={loading}
              error={error}
              onBook={setBook}
              onWindow={setWindowKey}
              onOpenResearch={() => setView("research")}
              onOpenSystem={() => setView("system")}
              onRetry={() => setRefreshToken((current) => current + 1)}
            />
          )}
          {view === "trades" && (
            <TradesView key={`${book}-${windowKey}`} book={book} windowKey={windowKey} />
          )}
          {view === "research" && (
            <ResearchView futures={futures} books={books} onOpenTrades={() => setView("trades")} />
          )}
          {view === "forward" && <ForwardView />}
          {view === "system" && (
            <SystemView
              system={system}
              book={book}
              onControl={handleControl}
              busy={controlBusy}
              actionError={controlError}
            />
          )}
        </main>

        <footer className="app-footer">
          <span>SkyQ RANGE / systematic ETF research</span>
          <span>Research build · not investment advice · paper before principal</span>
        </footer>
      </div>
    </div>
  );
}
