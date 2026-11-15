import { useEffect, useState } from "react";
import { getRunners } from "../api";
import { Icon } from "../components/Icons";
import type { RunnerCatalog } from "../types";

export function ForwardView() {
  const [catalog, setCatalog] = useState<RunnerCatalog | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getRunners(controller.signal)
      .then((payload) => {
        setCatalog(payload);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "Could not load runner configs");
      });
    return () => controller.abort();
  }, []);

  if (error) {
    return (
      <div className="view-stack">
        <div className="error-panel">
          <Icon name="alert" size={18} />
          <div>
            <strong>Runner catalog unavailable</strong>
            <p>{error}</p>
          </div>
        </div>
      </div>
    );
  }

  if (!catalog) {
    return (
      <div className="view-stack">
        <div className="empty-note">Loading runner configuration…</div>
      </div>
    );
  }

  return (
    <div className="view-stack">
      <section className="page-intro">
        <div>
          <span className="eyebrow">Forward test</span>
          <h1>Runner lab</h1>
          <p>
            Each JSON file under <code>{catalog.config_dir}</code> is an independent paper
            book. The shared LSE bridge owns the only WebSocket; every runner reads its
            cache and heartbeat. Change one file, restart the runner, and the next bar uses
            the new quantity, side filter, stop, hold limit or flat time.
          </p>
        </div>
        <div className="intro-stat">
          <span>Runner presets</span>
          <strong>{catalog.runners.length}</strong>
          <small>Check the file before starting</small>
        </div>
      </section>

      <div className="system-grid">
        <section className="panel">
          <header className="panel-head compact">
            <div>
              <span className="eyebrow">One connection</span>
              <h2>Start the shared bridge first</h2>
            </div>
            <Icon name="activity" size={19} />
          </header>
          <p className="panel-note">
            The bridge needs your own LSE key file. Runners never read it. On the free plan,
            use one symbol at a time unless your plan allows more than one connection.
          </p>
          <div className="code-line">
            <Icon name="database" size={15} />
            <code>python -m scripts.run_lse_bridge --symbol SPY --key-file &lt;key-file&gt;</code>
          </div>
          <div className="code-line">
            <Icon name="layers" size={15} />
            <code>python -m scripts.run_lse_runner --config configs/runners/spy-1m-paper.json</code>
          </div>
          <div className="code-line">
            <Icon name="play" size={15} />
            <code>python -m scripts.run_lse_stack --key-file &lt;key-file&gt;</code>
          </div>
          <p className="panel-note">
            The dashboard only reads; it never starts an LSE process or receives a key. The
            existing paper-monitor control remains on the System page.
          </p>
        </section>

        <section className="panel">
          <header className="panel-head compact">
            <div>
              <span className="eyebrow">Data sources</span>
              <h2>Shared one-minute cache</h2>
            </div>
            <Icon name="database" size={19} />
          </header>
          <div className="check-list">
            {catalog.data_sources.map((source) => (
              <div key={source.name}>
                <span className="check-dot on">
                  <Icon name="check" size={12} />
                </span>
                <span>{source.name}</span>
                <strong className="text-mint">enabled</strong>
              </div>
            ))}
          </div>
          <p className="panel-note">{catalog.data_sources[0]?.description}</p>
          <div className="code-line">
            <Icon name="clock" size={15} />
            <code>data/lse/shared/&lt;SYMBOL&gt;_1m_status.json</code>
          </div>
        </section>
      </div>

      <section className="panel">
        <header className="panel-head compact">
          <div>
            <span className="eyebrow">Presets on disk</span>
            <h2>Per-runner settings</h2>
          </div>
          <span className="badge badge-muted">edit JSON · restart runner</span>
        </header>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Runner</th>
                <th>Symbol</th>
                <th>Quantity</th>
                <th>Sides</th>
                <th>Stop</th>
                <th>Max hold</th>
                <th>Flat</th>
                <th>Broker</th>
                <th>File</th>
              </tr>
            </thead>
            <tbody>
              {catalog.runners.map((runner) => (
                <tr key={runner.file}>
                  <td><strong>{runner.runner_id ?? "invalid"}</strong></td>
                  <td>{runner.symbol ?? "—"}</td>
                  <td>{runner.quantity ?? "—"}</td>
                  <td>{runner.sides ?? "—"}</td>
                  <td>{runner.stop_ticks ?? "—"}</td>
                  <td>{runner.max_hold_bars ?? "—"}</td>
                  <td>{runner.flat_at ?? "—"}</td>
                  <td>{runner.broker ?? "—"}</td>
                  <td><code className="env-code">{runner.file}</code></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="panel">
        <header className="panel-head compact">
          <div>
            <span className="eyebrow">Execution boundary</span>
            <h2>What this public runner actually sends</h2>
          </div>
          <span className="badge badge-amber">simulated paper only</span>
        </header>
        <div className="table-scroll">
          <table className="data-table">
            <thead>
              <tr>
                <th>Broker</th>
                <th>Required names</th>
                <th>Status</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {catalog.brokers.map((broker) => (
                <tr key={broker.name}>
                  <td><strong>{broker.name}</strong></td>
                  <td><code className="env-code">{broker.required_env.join(", ") || "none"}</code></td>
                  <td>
                    <span className={`status-pill ${broker.implemented ? "is-on" : ""}`}>
                      <span className="status-dot" />
                      {broker.implemented ? "implemented" : "adapter only"}
                    </span>
                  </td>
                  <td className="muted small">{broker.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="panel-note">
          The tested path is the deterministic simulated paper engine. Broker adapters are
          documented so you can add your own execution layer without changing the signal
          source; never put an API secret in a runner JSON file.
        </p>
      </section>
    </div>
  );
}
