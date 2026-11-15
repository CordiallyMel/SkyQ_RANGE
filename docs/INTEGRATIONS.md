# Integrations and credentials

All integrations are optional. The core backtest, SQL seed and dashboard do not
need a broker or a data key. Each adapter is imported lazily; a missing package
or key produces a clear `not ready` status rather than breaking the rest of the
application.

Start with the doctor:

```bash
python -m scripts.check_integrations
```

It prints only whether environment names are present and whether local services
(for example IBKR Gateway or moomoo OpenD) are listening. It never prints a key,
password or account number.

## Environment names

Copy `.env.example` to `.env`. The file is ignored by Git.

| Name | Used by | Notes |
|---|---|---|
| `LSE_API_KEY` | LSE archive / optional live feed | vendor data key; not a broker |
| `DATABENTO_API_KEY` | Databento historical data | quote before download |
| `DATABENTO_DATASET` | Databento | default `EQUS.MINI` |
| `IB_HOST`, `IB_PORT`, `IB_CLIENT_ID` | IBKR bridge | default `127.0.0.1:4002` |
| `IB_ACCOUNT` | IBKR bridge | optional; never commit |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Alpaca paper | paper endpoint only |
| `MT5_PATH`, `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` | MetaTrader 5 | Windows only; use a demo |
| `MOOMOO_HOST`, `MOOMOO_PORT` | moomoo OpenD | default `127.0.0.1:11111`; read-only |
| `DATABASE_URL` | API / reporting | use a local role, not a superuser |
| `SKYQ_CONTROL_ENABLED` | API control | `true` only on a trusted loopback |
| `SKYQ_CONTROL_TOKEN` | API control | optional extra header secret |

## IBKR (primary paper execution)

`integrations/ibkr.py` wraps the optional `ib_async` package. It is the bridge
used by `src/main.py` and the paper monitor. Default is IB Gateway paper on port
4002. The runner:

1. validates the symbol against `LIVE_SYMBOLS` (SPY/QQQ only);
2. connects and requests the contract;
3. builds the bracket from the strategy plan;
4. only submits orders when `--execute` is present.

`--execute` does not switch the account type. Use a paper account for the public
demo and review the order plan before adding the flag.

## Alpaca (paper)

`integrations/alpaca.py` is a small REST adapter. It contains the paper base URL,
never a live URL. It has no retry on POST, so a timeout cannot become a duplicate
order. It is a demonstration of a second execution route, not the default.

## MetaTrader 5 (Windows)

`integrations/mt5.py` lazily imports the Windows `MetaTrader5` package. It is
not usable in Linux CI. Set `MT5_PATH` only if the terminal is not discoverable,
and use a demo login. The adapter does not contain credentials.

## moomoo / Futu OpenD (read-only)

`integrations/moomoo.py` connects to a locally running OpenD gateway. It is
read-only in this repository: quotes and contract discovery, no order methods.
Start OpenD yourself and set `MOOMOO_HOST` / `MOOMOO_PORT`.

## Databento (historical data)

`integrations/databento.py` requires the optional client:

```bash
pip install -e ".[databento]"
```

It exposes a quote and a download. A download refuses a quoted cost above the
configured budget. This is a data path for rebuilding the 1-minute history, not
an execution venue.

## LSE (London Strategic Edge): data, not a broker

LSE provides an archive and a live market-data socket. The API key is sent as the
`X-API-Key` header by the client; it is never placed in a URL, the dashboard or a
log.

### Archive client

`integrations/lse.py` contains `LseArchive`, which uses the documented base
`https://download.londonstrategicedge.com/v3`. It resolves the archive for a
symbol and date and writes an auditable parquet/CSV output. The original fuller
archive/feed implementation is preserved in `integrations/lse_feed.py`.

### Optional live feed

`integrations/lse_feed.py` contains the WebSocket client and five-minute bar
builder adapted from the SkyQ Sigma S work. It connects to
`wss://data-ws.londonstrategicedge.com` with the key header, consumes trade
ticks and builds bars. The feed is designed to warm up from the archive; it must
be started before the regular session because the protocol does not replay the
morning. The free plan allows one WebSocket connection per key.

The copied feed is provided as an integration reference. It has **not** been
validated against a live vendor account in this public build and it builds
**5-minute** bars, while RANGE consumes 1-minute bars. It is not wired into
`src/main.py`; use the archive client for 1-minute history and a validated
1-minute live adapter (or the IBKR 1-minute bars) for a forward test. Test any
live adapter with your own key before relying on it.

The archive client supports a 1-minute request:

```python
from datetime import date
from integrations.lse import LseArchive

archive = LseArchive.from_env()
frame = archive.bars("SPY", date(2026, 1, 2), date(2026, 11, 4), timeframe="1m")
```

### How a forward test fits together

```text
LSE archive ──► warmup bars ──┐
LSE live socket ──► tick bars ─┴──► RANGE engine ──► IBKR paper / Alpaca paper
```

LSE supplies data. IBKR, Alpaca or MT5 supplies execution. LSE cannot place a
trade. The repository has no live order endpoint and no key of its own.

## Adding a new integration safely

1. Put the adapter under `integrations/`, import optional packages lazily.
2. Add a status check that reports only `configured` / `reachable` / a safe error.
3. Add the names to `.env.example`, never a value.
4. Keep order submission in the existing runner, behind `--execute`.
5. Add the adapter to `integrations.doctor()` and document its boundary.
6. Add no network call to the test suite.
