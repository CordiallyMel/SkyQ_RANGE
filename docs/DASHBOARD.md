# Dashboard and runner customization

The dashboard is a React/Vite read model in `dashboard/`. The API is FastAPI in
`src/`. It is intentionally separate from the forward runner: the dashboard
cannot start a live LSE connection, read an API key, or submit an order.

## Run the dashboard

```bash
# terminal 1: API
python -m scripts.run_api

# terminal 2: UI
cd dashboard
npm install
npm run dev
```

Open the Vite URL (normally `http://127.0.0.1:5173`). The Vite proxy sends
`/api` to `http://127.0.0.1:8000`.

The **Forward** page reads `GET /api/runners`, which is a safe projection of
`configs/runners/*.json`. It shows the runner ID, symbol, quantity, stop, hold
limit, flat time, execution adapter and the required environment *names*. It
never returns values.

## Customize a paper runner

1. Copy a file under `configs/runners/`.
2. Change `runner_id`, `quantity`, `sides`, and only the knobs you intend to
   test.
3. Keep `broker` as `simulated` unless you have reviewed an execution adapter.
4. Start it:
   ```bash
   python -m scripts.run_lse_runner --config configs/runners/your-file.json
   ```
5. Refresh the Forward page. The new file appears automatically; no API restart
   is needed because the endpoint scans the directory on each request.

Numbers and examples are in [`FORWARD_TESTING.md`](FORWARD_TESTING.md).

## Connect your own platform

The supported public data source is `lse-shared-1m`. To add another source,
implement the read-only contract used by `SharedLseOneMinuteSource`:

- `start(now=..., timeout=...)`
- `poll()`
- `frame()` returning a New-York-indexed `open, high, low, close, volume` frame
- `latest_tick` when an execution adapter needs a quote
- `degraded_reason`, `status_payload()`, `stop()`

Then point a runner at it. Do not let a data adapter place orders.

For execution, consume the `PaperSession` signal/exit decisions through an
adapter. The platform's paper endpoint must be hard-coded or validated in the
adapter; a `paper=true` flag alone is not a safety boundary. Keep key names in
`.env` or the platform's secret store, never in the dashboard bundle or runner
JSON.

## Metrics per runner

The forward runner writes one CSV and one JSON status per `runner_id`:

- closed trades: P&L, costs, side, exit reason, channel width, bars held
- status: last bar, last price, open position, net, win rate, PF, drawdown,
  skipped-signal reasons and bridge health

To compare runners, use the same date range and the same bridge. A different
quantity changes dollar P&L but not the signal; a different stop or target
changes both. The published comparison uses fixed 1,000-share books and
closed-trade equity, so a 500-share runner is not directly comparable in
dollars.

## API surface

| Route | Purpose |
|---|---|
| `GET /api/health` | Database readiness |
| `GET /api/books` | Published book summaries |
| `GET /api/overview` | Equity, monthly, metrics |
| `GET /api/trades` | Paged trade ledger |
| `GET /api/research/futures` | Cross-asset gate |
| `GET /api/runners` | Safe runner config catalog |
| `GET /api/system` | DB counts, adapter imports, fixed paper-monitor control |
| `GET /api/integrations` | Environment-name readiness, no values |
| `POST /api/control/start|stop` | Fixed paper monitor only; loopback + token |

## What not to do

- Do not expose the API on `0.0.0.0` with control enabled.
- Do not add a generic command endpoint to the dashboard.
- Do not put a secret in `dashboard/src`, `configs/`, or a chart tooltip.
- Do not call a share paper run an options test.
- Do not present a degraded/partial LSE session as a valid forward test.
