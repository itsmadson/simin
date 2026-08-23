-- Trading and research bookkeeping.
DO $$ BEGIN
    CREATE TYPE run_mode AS ENUM ('backtest', 'paper', 'live');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS runs (
    id             uuid PRIMARY KEY,
    mode           run_mode NOT NULL,
    started_at     timestamptz NOT NULL DEFAULT now(),
    ended_at       timestamptz,
    code_sha       text NOT NULL,
    config_hash    text NOT NULL,
    data_snapshot  text,
    seed           integer,
    risk_profile   text NOT NULL,
    notes          text
);

CREATE TABLE IF NOT EXISTS signals (
    id             uuid PRIMARY KEY,
    run_id         uuid NOT NULL REFERENCES runs(id),
    ts             timestamptz NOT NULL,
    symbol_id      integer NOT NULL REFERENCES symbols(id),
    tf             text NOT NULL,
    direction      text NOT NULL,
    entry          numeric NOT NULL,
    stop           numeric NOT NULL,
    take_profits   jsonb NOT NULL DEFAULT '[]',
    probability    numeric,
    confidence     numeric,
    risk_reward    numeric,
    expected_value numeric,
    expected_cost  numeric NOT NULL,
    strategy       text NOT NULL,
    regime         text,
    holding_est    interval,
    features_ref   jsonb
);
CREATE INDEX IF NOT EXISTS signals_run_ts ON signals (run_id, ts DESC);

CREATE TABLE IF NOT EXISTS orders (
    id                uuid PRIMARY KEY,
    run_id            uuid NOT NULL REFERENCES runs(id),
    signal_id         uuid REFERENCES signals(id),
    venue_id          smallint NOT NULL REFERENCES venues(id),
    symbol_id         integer NOT NULL REFERENCES symbols(id),
    side              text NOT NULL,
    type              text NOT NULL,
    qty               numeric NOT NULL,
    price             numeric,
    status            text NOT NULL,
    client_order_id   text NOT NULL UNIQUE,   -- idempotency key for safe retries
    exchange_order_id text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    reject_reason     text
);

CREATE TABLE IF NOT EXISTS fills (
    id            uuid PRIMARY KEY,
    order_id      uuid NOT NULL REFERENCES orders(id),
    ts            timestamptz NOT NULL,
    price         numeric NOT NULL,
    qty           numeric NOT NULL,
    fee           numeric NOT NULL,
    fee_asset     text NOT NULL,
    is_maker      boolean NOT NULL,
    slippage_bps  numeric
);

CREATE TABLE IF NOT EXISTS positions (
    id                uuid PRIMARY KEY,
    run_id            uuid NOT NULL REFERENCES runs(id),
    symbol_id         integer NOT NULL REFERENCES symbols(id),
    side              text NOT NULL,
    qty               numeric NOT NULL,
    avg_entry         numeric NOT NULL,
    stop              numeric,
    opened_at         timestamptz NOT NULL,
    closed_at         timestamptz,
    realized_pnl_irt  numeric,
    realized_pnl_usdt numeric,          -- both currencies, always (docs/01 §0.1)
    fees_paid         numeric NOT NULL DEFAULT 0,
    strategy          text,
    regime            text
);

CREATE TABLE IF NOT EXISTS equity_curve (
    run_id       uuid NOT NULL REFERENCES runs(id),
    ts           timestamptz NOT NULL,
    balance_irt  numeric NOT NULL,
    equity_irt   numeric NOT NULL,
    equity_usdt  numeric NOT NULL,
    unrealized   numeric NOT NULL DEFAULT 0,
    drawdown     numeric NOT NULL DEFAULT 0,
    exposure     numeric NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, ts)
);

CREATE TABLE IF NOT EXISTS risk_events (
    id      bigserial PRIMARY KEY,
    run_id  uuid REFERENCES runs(id),
    ts      timestamptz NOT NULL DEFAULT now(),
    kind    text NOT NULL,
    detail  jsonb NOT NULL DEFAULT '{}'
);

-- every access to the locked out-of-sample period is recorded; an OOS set you
-- peeked at twice is no longer out-of-sample (docs/03 §3)
CREATE TABLE IF NOT EXISTS oos_access_log (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    dataset    text NOT NULL,
    reason     text NOT NULL,
    code_sha   text NOT NULL
);
