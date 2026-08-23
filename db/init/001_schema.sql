-- Simin core schema. Applied on first container start.
CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS venues (
    id          smallserial PRIMARY KEY,
    code        text UNIQUE NOT NULL,
    name        text NOT NULL,
    enabled     boolean NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS symbols (
    id            serial PRIMARY KEY,
    venue_id      smallint NOT NULL REFERENCES venues(id),
    symbol        text NOT NULL,
    base          text NOT NULL,
    quote         text NOT NULL,
    price_tick    numeric NOT NULL DEFAULT 0,
    qty_step      numeric NOT NULL DEFAULT 0,
    min_notional  numeric NOT NULL DEFAULT 0,
    -- point-in-time listing window: backtests must not see a symbol before it
    -- existed or after it was delisted (survivorship bias guard)
    listed_at     timestamptz,
    delisted_at   timestamptz,
    UNIQUE (venue_id, symbol)
);

CREATE TABLE IF NOT EXISTS ohlcv (
    symbol_id     integer NOT NULL REFERENCES symbols(id),
    tf            text    NOT NULL,
    ts            timestamptz NOT NULL,          -- bar OPEN time, UTC
    open          numeric NOT NULL,
    high          numeric NOT NULL,
    low           numeric NOT NULL,
    close         numeric NOT NULL,
    volume        numeric NOT NULL,
    quote_volume  numeric,
    trades        integer,
    ingest_time   timestamptz NOT NULL DEFAULT now(),   -- when *we* learned it
    CONSTRAINT ohlcv_sane CHECK (high >= low AND open BETWEEN low AND high
                                 AND close BETWEEN low AND high AND volume >= 0),
    PRIMARY KEY (symbol_id, tf, ts)
);
SELECT create_hypertable('ohlcv', 'ts', chunk_time_interval => interval '7 days',
                         if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS ohlcv_symbol_tf_ts ON ohlcv (symbol_id, tf, ts DESC);

CREATE TABLE IF NOT EXISTS trades_raw (
    symbol_id  integer NOT NULL REFERENCES symbols(id),
    ts         timestamptz NOT NULL,
    price      numeric NOT NULL,
    qty        numeric NOT NULL,
    side       text NOT NULL,
    trade_id   text NOT NULL
);
SELECT create_hypertable('trades_raw', 'ts', chunk_time_interval => interval '1 day',
                         if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS orderbook_snap (
    symbol_id integer NOT NULL REFERENCES symbols(id),
    ts        timestamptz NOT NULL,
    bids      jsonb NOT NULL,
    asks      jsonb NOT NULL,
    levels    integer NOT NULL
);
SELECT create_hypertable('orderbook_snap', 'ts', chunk_time_interval => interval '1 day',
                         if_not_exists => TRUE);

-- derivatives context from global venues: the only non-price-derived signal
-- family available (funding / OI / liquidations), see docs/01 §1.1
CREATE TABLE IF NOT EXISTS derivs (
    symbol           text NOT NULL,
    ts               timestamptz NOT NULL,
    funding          numeric,
    open_interest    numeric,
    long_short_ratio numeric,
    liq_long         numeric,
    liq_short        numeric,
    PRIMARY KEY (symbol, ts)
);
SELECT create_hypertable('derivs', 'ts', chunk_time_interval => interval '30 days',
                         if_not_exists => TRUE);

-- the Toman leg: every IRT pair decomposes into crypto beta x USDT/IRT
CREATE TABLE IF NOT EXISTS fx_irt (
    ts               timestamptz PRIMARY KEY,
    usdt_irt         numeric NOT NULL,
    source           text NOT NULL,
    implied_premium  numeric
);

CREATE TABLE IF NOT EXISTS data_quality_log (
    id        bigserial PRIMARY KEY,
    symbol_id integer REFERENCES symbols(id),
    tf        text,
    ran_at    timestamptz NOT NULL DEFAULT now(),
    n_bars    integer NOT NULL,
    issues    jsonb NOT NULL
);

DO $$ BEGIN
    PERFORM add_compression_policy('ohlcv', INTERVAL '90 days');
    PERFORM add_compression_policy('trades_raw', INTERVAL '30 days');
EXCEPTION WHEN others THEN NULL;
END $$;
