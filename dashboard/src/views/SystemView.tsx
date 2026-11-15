import { Icon } from "../components/Icons";
import type { BookKey, SystemInfo } from "../types";
import { dateTime, number } from "../lib/format";

const BROKERS = [
  {
    key: "ibkr",
    name: "Interactive Brokers",
    role: "Paper execution bridge",
    env: "IB_HOST, IB_PORT, IB_CLIENT_ID",
    note: "Default is the paper Gateway on 4002. Live ports exist but are not enabled by this API.",
  },
  {
    key: "alpaca",
    name: "Alpaca",
    role: "Paper equity execution",
    env: "ALPACA_API_KEY, ALPACA_SECRET_KEY",
    note: "The adapter is paper-only by construction; no live endpoint is called.",
  },
  {
    key: "mt5",
    name: "MetaTrader 5",
    role: "Windows CFD bridge",
    env: "MT5_PATH, MT5_LOGIN, MT5_PASSWORD, MT5_SERVER",
    note: "Windows-only. Install the MT5 terminal and Python package; no Linux server support.",
  },
  {
    key: "moomoo",
    name: "moomoo / Futu",
    role: "Read-only OpenD feed",
    env: "MOOMOO_HOST, MOOMOO_PORT",
    note: "Requires OpenD listening on 127.0.0.1:11111. The adapter does not submit orders.",
  },
  {
    key: "databento",
    name: "Databento",
    role: "Historical 1-minute data",
    env: "DATABENTO_API_KEY",
    note: "Used to build research series; not a broker and never used for live routing.",
  },
  {
    key: "lse",
    name: "London Strategic Edge",
    role: "Market data API / stream",
    env: "LSE_API_KEY",
    note: "Data vendor, not a broker. Forward-test quotes and archives only.",
  },
];

export function SystemView({
  system,
  book,
  onControl,
  busy,
  actionError,
}: {
  system: SystemInfo | null;
  book: BookKey;
  onControl: (action: "start" | "stop") => Promise<void>;
  busy: boolean;
  actionError: string | null;
}) {
  if (!system) {
    return (
      <div className="view-stack">
        <div className="error-panel">
          <Icon name="alert" size={18} />
          <div>
            <strong>System read model unavailable</strong>
            <p>Start the FastAPI service and seed the database, then refresh this view.</p>
          </div>
        </div>
      </div>
    );
  }

  const control = system.control;
  const db = system.database;

  return (
    <div className="view-stack">
      <section className="page-intro">
        <div>
          <span className="eyebrow">Operations</span>
          <h1>System & control</h1>
          <p>
            The control center is deliberately narrow. Research and reporting are read-only;
            the only mutation is a fixed paper-monitor process. Live order submission is not
            exposed through the API.
          </p>
        </div>
        <div className="intro-stat">
          <span>Database</span>
          <strong>{db.dialect}</strong>
          <small>{number(db.trades)} trades in SQL</small>
        </div>
      </section>

      <div className="system-grid">
        <section className="panel database-panel">
          <header className="panel-head compact">
            <div>
              <span className="eyebrow">SQL read model</span>
              <h2>Database is live</h2>
            </div>
            <span className={`badge ${db.dialect === "postgresql" ? "badge-mint" : "badge-amber"}`}>
              {db.dialect}
            </span>
          </header>
          <div className="system-counts">
            <div><span>Instruments</span><strong>{number(db.instruments)}</strong></div>
            <div><span>Runs</span><strong>{number(db.runs)}</strong></div>
            <div><span>Trades</span><strong>{number(db.trades)}</strong></div>
            <div><span>Equity points</span><strong>{number(db.equity_points)}</strong></div>
          </div>
          <div className="code-line">
            <Icon name="database" size={15} />
            <code>{db.url}</code>
          </div>
          <p className="panel-note">
            The committed sample is enough to render the dashboard. For the full research
            record, run <code>scripts/load_ledgers.py</code> against PostgreSQL.
          </p>
        </section>

        <section className="panel control-panel">
          <header className="panel-head compact">
            <div>
              <span className="eyebrow">Paper monitor</span>
              <h2>{control.running ? "Process running" : "Process idle"}</h2>
            </div>
            <span className={`badge ${control.running ? "badge-mint" : control.enabled ? "badge-amber" : "badge-muted"}`}>
              {control.running ? "monitoring" : control.enabled ? "enabled" : "disabled"}
            </span>
          </header>
          <div className="control-body">
            <div className={`control-orb ${control.running ? "is-running" : ""}`}>
              <Icon name={control.running ? "activity" : "play"} size={26} />
            </div>
            <div>
              <strong className="control-symbol">{control.running ? control.symbol : book} paper monitor</strong>
              <p>
                {control.running
                  ? `Started ${dateTime(control.started_at)}. PID ${control.pid ?? "—"}.`
                  : "Starts the fixed src.main paper loop. No --execute flag is ever added."}
              </p>
            </div>
          </div>
          {actionError && (
            <div className="inline-error">
              <Icon name="alert" size={15} />
              {actionError}
            </div>
          )}
          <div className="control-actions">
            <button
              className="button button-primary"
              disabled={busy || !control.enabled || control.running}
              onClick={() => void onControl("start")}
            >
              <Icon name="play" size={15} />
              Start paper monitor
            </button>
            <button
              className="button button-ghost"
              disabled={busy || !control.enabled || !control.running}
              onClick={() => void onControl("stop")}
            >
              <Icon name="stop" size={15} />
              Stop
            </button>
          </div>
          <p className="panel-note">
            {control.note} Set <code>SKYQ_CONTROL_ENABLED=true</code> and optionally
            <code>SKYQ_CONTROL_TOKEN</code> to opt in. The endpoint is loopback-only and
            requires the dashboard control header.
          </p>
        </section>
      </div>

      <section className="panel broker-panel">
        <header className="panel-head compact">
          <div>
            <span className="eyebrow">Credential surface</span>
            <h2>Adapters and environment names</h2>
          </div>
          <span className="badge badge-muted">values never leave .env</span>
        </header>
        <div className="table-scroll">
          <table className="data-table broker-table">
            <thead>
              <tr>
                <th>Integration</th>
                <th>Role</th>
                <th>Required names</th>
                <th>This machine</th>
                <th>Boundary</th>
              </tr>
            </thead>
            <tbody>
              {BROKERS.map((broker) => {
                const configured = system.brokers_configured[broker.key] === true;
                return (
                  <tr key={broker.key}>
                    <td><strong>{broker.name}</strong></td>
                    <td className="muted">{broker.role}</td>
                    <td><code className="env-code">{broker.env}</code></td>
                    <td>
                      <span className={`status-pill ${configured ? "is-on" : ""}`}>
                        <span className="status-dot" />
                        {configured ? "configured" : "not set"}
                      </span>
                    </td>
                    <td className="muted small">{broker.note}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      <div className="system-grid lower">
        <section className="panel">
          <header className="panel-head compact">
            <div>
              <span className="eyebrow">Runtime</span>
              <h2>Adapter imports</h2>
            </div>
            <Icon name="system" size={19} />
          </header>
          <div className="check-list">
            {Object.entries(system.packages).map(([name, available]) => (
              <div key={name}>
                <span className={`check-dot ${available ? "on" : ""}`}>
                  {available ? <Icon name="check" size={12} /> : <Icon name="close" size={12} />}
                </span>
                <span>{name}</span>
                <strong className={available ? "text-mint" : "text-muted"}>
                  {available ? "imports" : "not installed"}
                </strong>
              </div>
            ))}
          </div>
          <p className="panel-note">
            Optional packages stay optional. The research engine only needs numpy, pandas and
            pyarrow; the API adds FastAPI, SQLAlchemy and psycopg.
          </p>
        </section>

        <section className="panel">
          <header className="panel-head compact">
            <div>
              <span className="eyebrow">Audit trail</span>
              <h2>Recent control events</h2>
            </div>
            <Icon name="clock" size={19} />
          </header>
          <div className="audit-list">
            {system.latest_audit.length === 0 && <div className="empty-note">No events recorded.</div>}
            {system.latest_audit.map((event, index) => (
              <div key={`${event.action}-${index}`} className="audit-row">
                <span className="audit-dot" />
                <div>
                  <strong>{event.action}</strong>
                  <span>{event.actor} · {dateTime(event.at)}</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>

      <div className="callout callout-neutral">
        <Icon name="shield" size={19} />
        <div>
          <strong>Security boundary</strong>
          <p>
            No API key, password or token is committed or sent to the browser. The dashboard
            only sees the names of required environment variables. Control endpoints are
            loopback-only and disabled by default. The repository's <code>.env.example</code>
            documents every supported integration.
          </p>
        </div>
      </div>
    </div>
  );
}
