-- Optional market-data store. The reporting tables in 001_schema.sql are
-- intentionally separate: a market archive can be many gigabytes, while the
-- reporting database stays small enough to ship and query interactively.
--
-- PostgreSQL 14+:
--   psql "$DATABASE_URL" -f sql/003_market_bars.sql

BEGIN;

CREATE TABLE IF NOT EXISTS market_bars (
    dataset             VARCHAR(80) NOT NULL,
    symbol              VARCHAR(24) NOT NULL,
    "ts"                TIMESTAMPTZ NOT NULL,
    open                DOUBLE PRECISION NOT NULL,
    high                DOUBLE PRECISION NOT NULL,
    low                 DOUBLE PRECISION NOT NULL,
    close               DOUBLE PRECISION NOT NULL,
    volume              DOUBLE PRECISION NOT NULL DEFAULT 0,
    source              VARCHAR(240) NOT NULL DEFAULT '',
    PRIMARY KEY (dataset, symbol, "ts")
);

CREATE INDEX IF NOT EXISTS idx_market_bars_symbol_ts
    ON market_bars (symbol, "ts");

COMMENT ON TABLE market_bars IS
    'Optional user-supplied 1-minute bars. Load with scripts.import_market_data.';

COMMIT;
