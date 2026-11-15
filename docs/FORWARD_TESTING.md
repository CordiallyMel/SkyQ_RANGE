# One-minute forward testing with LSE

This is the practical path for running the RANGE signal on **your own LSE key**
without sharing the key with a broker process, and without opening one
WebSocket per runner.

The public implementation is deliberately **paper-first**. It produces a
deterministic simulated paper journal. Broker adapters exist in the repository,
but the LSE runner does not silently submit live orders. Read the
[execution boundary](#execution-boundary) before connecting an account.

## 1. What runs

```
                    one LSE WebSocket + one key
                                  │
                       ┌──────────▼──────────┐
                       │  run_lse_bridge.py  │
                       │  warmup + 1m bars   │
                       └──────┬───────┬──────┘
                              │       │
             warmup parquet   │       │   status JSON + live CSV
                              ▼       ▼
                    ┌───────────────────────┐
                    │ SharedLseOneMinute    │
                    │ Source (read-only)     │
                    └──────┬────────┬────────┘
                           │        │
                     ┌─────▼──┐  ┌──▼─────┐
                     │ runner │  │ runner │   ... as many as you want
                     │ SPY    │  │ QQQ    │
                     └────────┘  └────────┘
```

- The **bridge** is the only process that reads the LSE key or opens the
  WebSocket. It downloads a one-minute archive warmup through yesterday, builds
  completed one-minute bars from the live tick stream, appends them to a durable
  CSV, and writes an atomic heartbeat.
- The **runner** reads the warmup parquet, the live CSV and the heartbeat. It
  never needs a key. You can run several runners against one bridge.
- A runner keeps its own position, journal, quantity and metrics. Two runners
  can trade the same symbol with different sizes or sides; they do not share a
  position.

The signal is the same `src.strategy_logic` engine used by the backtest:
20% channel-edge entry, channel-fraction target, symbol-preset stop, 15:55 ET
flat, next-bar-open fill, one tick of entry/exit slippage and the instrument's
commission profile. The paper engine is in `src/paper_session.py`.

## 2. Before you start

1. Python 3.11–3.13 and the repository environment:
   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. An LSE API key with archive and WebSocket access.
3. A paper account only if you later add an execution adapter. The included
   runner needs **no broker account**.

Do not put the LSE key in a runner JSON, a notebook, a screenshot, or a Git
commit. The bridge accepts a key file; `.env` is also read by the underlying
client but a file is easier to protect.

## 3. Start the bridge

Create a key file outside the repository (or in an ignored path):

```bash
mkdir -p ~/.config/lse
printf '%s' 'YOUR_LSE_KEY' > ~/.config/lse/api-key
chmod 600 ~/.config/lse/api-key
```

Windows PowerShell:

```powershell
New-Item -ItemType Directory -Force "$HOME\.config\lse" | Out-Null
Set-Content -NoNewline "$HOME\.config\lse\api-key" "YOUR_LSE_KEY"
```

Start one symbol:

```bash
python -m scripts.run_lse_bridge --symbol SPY \
  --key-file "$HOME/.config/lse/api-key" \
  --cache-dir data/lse
```

The first run downloads the warmup. It can take a minute depending on the
archive. A healthy log includes a line like:

```text
LSE 1m source ready SPY | warmup=... bars through ... | today_cache=... | ...
bridge ready: ... | today=... bars | started_before_open=True
```

The bridge writes:

| Path | Purpose |
|---|---|
| `data/lse/SPY_1m_warmup.parquet` | Completed archive bars through yesterday |
| `data/lse/SPY_1m_warmup.json` | Cache metadata and row count |
| `data/lse/live/SPY_YYYY-MM-DD_1m.csv` | Append-only live completed bars |
| `data/lse/shared/SPY_1m_status.json` | Readiness, heartbeat, tick, degraded reason |

**Start before 09:25 ET.** LSE cannot replay the current session from the
archive; starting after 09:30 makes the day degraded. The bridge will say so in
the status file. Do not treat a partial morning as a valid forward test.

On the **free LSE plan**, one key has one concurrent WebSocket. Run one bridge
at a time unless your plan explicitly allows more. A second bridge failing to
connect is usually the plan limit, not this code.

## 4. Start runners

In a second terminal, with the bridge running:

```bash
python -m scripts.run_lse_runner \
  --config configs/runners/spy-1m-paper.json \
  --cache-dir data/lse
```

You should see a seeded line, then one status line per poll. The runner writes:

- `data/forward/spy-1m-paper.csv` — closed paper trades
- `data/forward/spy-1m-paper.status.json` — metrics, position, source health
- optional console log from your shell redirection

Run QQQ against the same kind of bridge only if your key permits it. The
included QQQ config is independent; it does not share SPY's positions.

### Start everything together

The stack supervisor starts one bridge per symbol in the configs, waits for
each heartbeat, then launches every runner:

```bash
python -m scripts.run_lse_stack \
  --configs configs/runners/spy-1m-paper.json configs/runners/qqq-1m-paper.json \
  --key-file "$HOME/.config/lse/api-key"
```

Logs go to `data/forward/logs/`. If the key is free-plan single-connection and
you ask for SPY and QQQ, the second bridge will fail; use one symbol or a
permitted plan. `--no-bridges` starts runners only against an existing bridge.
`--no-runners` starts bridges only.

**The supervisor does not run as a Windows service.** If the machine sleeps,
the session ends or the terminal closes, the processes stop. That is preferable
to a zombie paper book with stale quotes, but it means you must supervise it.

## 5. Configure your own paper book

Copy a preset and edit it:

```bash
cp configs/runners/spy-1m-paper.json configs/runners/spy-500-long.json
```

```json
{
  "runner_id": "spy-500-long",
  "symbol": "SPY",
  "broker": "simulated",
  "data_source": "lse-shared-1m",
  "quantity": 500,
  "sides": "long",
  "stop_ticks": 100.0,
  "max_hold_bars": 120,
  "entry_zone": 0.20,
  "min_channel_pct": 0.0020,
  "tp_channel_pct": 0.20,
  "atr_filter": false,
  "atr_min": 4.0,
  "session": "rth",
  "flat_at": "15:55",
  "slippage_ticks": 1.0,
  "poll_seconds": 5.0
}
```

| Field | Meaning |
|---|---|
| `runner_id` | Unique name for the journal/status files |
| `symbol` | `SPY` or `QQQ`; futures are research-only |
| `quantity` | Shares (or contracts only if a future is enabled by your own fork) |
| `sides` | `both`, `long` or `short` |
| `stop_ticks` | Stop distance beyond the channel edge in ticks |
| `max_hold_bars` | Time stop in one-minute bars |
| `entry_zone` | Fraction of channel treated as the edge zone (0–0.5) |
| `min_channel_pct` | Relative minimum channel width; preset default |
| `tp_channel_pct` | Target as a fraction of channel width |
| `atr_filter` | Optional volatility gate; canonical book is `false` |
| `session` | `rth`, `all`, `core` or `evening` |
| `flat_at` | ET time after which no new entry is opened |
| `slippage_ticks` | One tick is the research assumption |
| `poll_seconds` | How often the runner reads the shared files |

The canonical book is **1,000 shares**, both sides, the symbol preset stop/time stop, 20% entry zone, 20% channel target, RTH, 15:55 flat. Keep those values if you want to compare your paper journal with the published research. Change one thing at a time if you are testing a variant.

The live bridge builds only regular-hours one-minute bars. The canonical research file may contain premarket/after-hours rows, and those rows can influence the channel warmup even though entries are RTH-only. A forward journal is therefore the same **decision** engine but not a bit-for-bit replay of the canonical backtest; expect small differences in channel boundaries and trade selection. The RTH-only paper replay is the closer comparison for the live path.

There is **no options execution path**. The option data is research evidence;
option contracts have different pricing, Greeks, expiry and assignment rules.
Do not label a share-based paper run as an options test.

## 6. Offline validation before the open

You can replay a historical one-minute file through the exact paper engine:

```bash
python -m scripts.paper_replay \
  --config configs/runners/spy-1m-paper.json \
  --data /path/to/SPY_1m.parquet \
  --start 2026-09-01 --end 2026-11-04 \
  --output-dir reports/paper_replay
```

This is the right place to check a quantity change, a stop variant or a new
data file. It does not prove the live feed will be complete; it proves the
bracket and journal behave as expected on completed bars.

The committed example in [`../reports/paper_replay/`](../reports/paper_replay/)
is a 282-trade RTH replay over 2026-10-01..2026-11-04. Read its README before
comparing it with the canonical 24-year result; it has a 500-bar warmup and is
not a live LSE session.

## 7. Failure modes and what they mean

| Symptom | Meaning | Action |
|---|---|---|
| `shared ... bridge is not publishing` | No bridge status file | Start the bridge; check its log |
| `heartbeat is stale` | Bridge stopped or machine slept | Restart the bridge for a new session |
| `started at or after 09:30` | Partial session; opening ticks missed | Do not count the day; start earlier tomorrow |
| `DEGRADED: ... WebSocket` | Missing ticks during protected hours | Stop the runner; a partial tape is not a test |
| `session_date` mismatch | Yesterday's bridge is still on disk | Restart the bridge on the current date |
| `invalid_bracket` in skipped counts | Gap made the stop/target geometry invalid | Expected; no trade was invented |
| Runner exits with code 3 | Degraded source by default | Restart bridge; use `--allow-degraded` only for debugging |

`--allow-degraded` is intentionally not the default. A paper result built on a
hole in the tape is worse than no result.

## 8. Execution boundary

`--broker simulated` is the tested path. The included adapters and their honest
status:

| Adapter | What exists | What this runner does |
|---|---|---|
| Simulated paper | `src/paper_session.py` | Fills, costs, stops, target, time/flat exits, journal, metrics |
| Alpaca paper | `integrations/alpaca.py` bracket submission | Not wired to the LSE runner; would need a reviewed order lifecycle |
| IBKR paper | `src/main.py` and `integrations/ibkr.py` | Separate paper-monitor path; not wired to the shared 1m source |
| MT5 | `integrations/mt5.py` account/quote adapter | No order submission here |
| moomoo | `integrations/moomoo.py` read-only OpenD quotes | Never submits orders |

If you add an execution adapter, keep the signal source unchanged and make the
adapter consume an `EntryPlan`/exit event. Required safeguards:

- idempotent client order IDs; never resubmit after an unknown response
- paper endpoint enforced in code, not only in configuration
- server-side bracket/OCO or an equivalent monitored stop
- position reconciliation before every new entry
- no secrets in JSON, logs or the dashboard
- a kill switch and a daily loss stop

The repository's existing adapters and their environment names are documented
in [`INTEGRATIONS.md`](INTEGRATIONS.md). A broker key is not a data key.

## 9. Verify the installation

```bash
python -m pytest -q
python -m scripts.backtest_range --symbol SPY --quantity 1000 \
  --data /path/to/SPY_1m_24y.parquet --start 2003-09-10 --end 2026-11-04
```

The baseline should print 67,271 trades and net +$1,658,827.85 on the canonical
file. Small differences in a vendor's history are data differences, not a code
pass.

## 10. What this does not prove

- No real LSE key or broker paper account is included, so the public repository
  cannot claim a live session was executed.
- The paper fill model is bar-based; it does not model queue position, partial
  fills, halts, LULD, borrow or financing.
- The strategy is selected on the available history. Forward results are the
  only clean out-of-sample evidence, and one session is not statistical proof.
- LSE terms, market-data redistribution and broker paper-account rules are your
  responsibility.
