# Contributing

Thanks for taking a look. This is a research repository first; changes should
make the strategy, the evidence or the reporting easier to verify.

## Development setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env
python -m scripts.init_db          # SQLite fallback by default
pytest -q
cd dashboard && npm ci && npm run build
```

## Rules

1. **No secrets.** Do not add a real key, account number, password or `.env`
   file. Use placeholders and read names from the environment.
2. **No cherry-picked performance.** If a result changes, regenerate the
   published data and update the canonical report. Do not silently change a
   window, quantity or cost assumption.
3. **Keep the execution model causal.** Decisions use completed bars; entries are
   at the next bar open; a stop-first assumption is the default when a bar touches
   both levels.
4. **Do not add a futures live path.** The cross-asset gate is negative after
   costs. Futures stay research-only.
5. **Keep the control surface narrow.** Do not add an arbitrary-command,
   order-submission or live-broker endpoint to the local API.
6. **Add a test for behavior changes.** A small focused test is better than a
   large fixture.
7. **Explain why.** A pull request should state the hypothesis, the measurement
   and the result, including negative results.

## Code style

Python formatting is intentionally simple and the project targets 3.11+.
TypeScript is built in strict mode. Keep comments directed at the next reader,
not at the author.

## Reporting a problem

Open a GitHub issue with the command, expected result and actual result. If the
issue involves a credential, follow [SECURITY.md](SECURITY.md) instead.
