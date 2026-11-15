# Security policy

## Scope

This repository is research software with an optional paper-execution bridge.
It is not a hosted multi-tenant service. The security-sensitive surfaces are:

- local API control endpoints (`/api/control/*`);
- optional broker and data adapters;
- any operator-supplied `.env` file;
- the local PostgreSQL/SQLite reporting database.

## Secrets

Never commit any of the following:

- `LSE_API_KEY`, `DATABENTO_API_KEY`;
- `ALPACA_API_KEY` / `ALPACA_SECRET_KEY`;
- `IB_ACCOUNT`, broker passwords, MT5 login credentials;
- database passwords or full `DATABASE_URL` values containing a password;
- copied `.env`, `.env.*`, `.pem`, `.key` or account statements.

`.env` is ignored by Git. `.env.example` contains only names and placeholders.
The dashboard and API return environment-variable names, never values. The API
masks the password portion of the database URL in `/api/system`.

If a credential is exposed, revoke or rotate it at the provider first. Removing
it from the working tree does not remove it from Git history; rewrite history or
replace the repository if necessary.

## Control endpoint boundary

- Disabled by default (`SKYQ_CONTROL_ENABLED=false`).
- Binds to loopback only.
- Requires `X-SkyQ-Control: 1`.
- Requires `SKYQ_CONTROL_TOKEN` when it is set.
- Allows only the fixed `python -m src.main` paper-monitor command; it never
  accepts a user-supplied command or adds `--execute`.
- Records an audit event in SQL.

Do not expose port 8000 to a public network with control enabled. The API has no
authentication layer for multi-user deployment and is intended for a local,
single-operator workstation.

## Reporting

Please use GitHub's private security advisory feature for this repository. Do
not open a public issue containing a live credential, account number or broker
token. Include the affected commit, a minimal reproduction, and whether the
issue affects research-only paths or optional execution adapters.

## Supported versions

Only the current `main` branch is supported. The project is research software;
there is no SLA.
