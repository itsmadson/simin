# Simin — Architecture

## 1. Principles
1. **The Risk Engine is the last word.** Every order, from every source (rules, ML, manual, arbitrage), passes through it. The AI cannot bypass it — it is a separate process boundary, not a function call the strategy could skip.
2. **Backtest, paper and live run the *same* strategy code**, differing only in the injected `Clock` and `ExchangeAdapter`. If code branches on mode, it is a bug.
3. **Point-in-time correctness everywhere.** Every stored row has `event_time` (when it happened) and `ingest_time` (when we learned it). Features may only read rows with `event_time <= t` *and* `ingest_time <= t`.
4. **Everything is reproducible.** Every run records code SHA, config hash, data snapshot id, random seed.

## 2. Component diagram

```
                     ┌──────────────────────────────┐
                     │ Next.js Dashboard (fa/en, RTL)│
                     └───────────────┬───────────────┘
                                     │ REST + WS
                     ┌───────────────▼───────────────┐
                     │  FastAPI  (read-mostly API)   │
                     └───────────────┬───────────────┘
                                     │
   ┌───────────────┬─────────────────┼──────────────────┬──────────────────┐
   │               │                 │                  │                  │
┌──▼─────────┐ ┌───▼────────┐ ┌──────▼──────┐ ┌─────────▼────────┐ ┌───────▼──────┐
│ Market Data│ │  Feature   │ │  Regime +   │ │  Strategy /      │ │  Backtest &  │
│ Ingestors  │→│  Engine    │→│  ML Service │→│  Signal Engine   │ │  Research    │
│ (WS+REST)  │ │ (Polars)   │ │ (LightGBM)  │ │  (ensemble)      │ │  (Optuna/MLflow)
└──┬─────────┘ └────────────┘ └─────────────┘ └─────────┬────────┘ └──────────────┘
   │                                                     │  Signal
   │ raw ticks/bars                          ┌───────────▼────────────┐
   │                                         │      RISK ENGINE       │  ← hard gate
   │                                         │  sizing · limits ·     │
   │                                         │  kill-switch · circuit │
   │                                         └───────────┬────────────┘
   │                                                     │ Order intent
   │                                         ┌───────────▼────────────┐
   │                                         │   Execution Engine     │
   │                                         │ (routing, slicing,     │
   │                                         │  retries, reconcile)   │
   │                                         └───────────┬────────────┘
   │                                                     │
   │                          ┌──────────────────────────┼──────────────────────┐
   │                          │              ExchangeAdapter (ABC)              │
   │                          ├──────────┬──────────┬───────────┬───────────────┤
   │                          │  Paper   │ CSVReplay│ PublicData│ *plugin: local│
   │                          └──────────┴──────────┴───────────┴───────────────┘
   ▼
TimescaleDB (hypertables) · Redis (hot state, locks, pubsub) · Parquet/S3 (cold research data)
```

Processes (each its own container, so one crash never takes the trader with it):
`api` · `ingestor` · `worker` (Arq) · `scheduler` · `trader` (single leader-elected loop) · `frontend` · `postgres` · `redis`.

**The trader loop is singleton by construction** — a Redis lease/leader lock. Two trader processes = double-sized positions = the classic account-killer.

## 3. Tech stack decision

| Layer | Choice | Why (and what was rejected) |
|---|---|---|
| Language | Python 3.12, fully typed, `mypy --strict` on core | ecosystem wins; latency isn't the edge at 1h–4h |
| Data frames | **Polars** for research/backtest, pandas only at boundaries | 5–20× faster on the feature pipeline; lazy engine catches schema errors early |
| API | FastAPI + Pydantic v2 | typed contracts shared with TS via generated OpenAPI |
| Storage | **PostgreSQL + TimescaleDB** hypertables + continuous aggregates | best fit for OHLCV/trades; compression on old chunks; SQL for research. Rejected InfluxDB (weaker joins), ClickHouse (great, but ops overhead for one user) |
| Cold data | Parquet (partitioned by symbol/date), local or S3-compatible | fast research reads, cheap archive |
| Cache/bus | Redis (streams + pubsub + locks) | one dependency doing hot state, leader lock, and fan-out |
| Jobs | **Arq** (async, Redis-native) over Celery | lighter, async-first, fewer moving parts |
| ML | scikit-learn, **LightGBM**, Optuna, MLflow | see research doc |
| DL (later) | PyTorch | phase 7 only |
| Migrations | Alembic | |
| Logging | structlog → JSON; Prometheus metrics; Grafana | |
| Frontend | Next.js + TS + Tailwind + shadcn/ui + **TradingView Lightweight Charts** | i18n via next-intl, **full RTL for Persian**, Vazirmatn font |
| Deploy | Docker Compose (`docker compose up -d`) | single-box friendly; k8s unnecessary |

## 4. Data schema (core tables)

```sql
-- symbols & venue universe (point-in-time!)
CREATE TABLE venues(id smallserial PRIMARY KEY, code text UNIQUE, name text, enabled bool);
CREATE TABLE symbols(
  id serial PRIMARY KEY, venue_id smallint REFERENCES venues(id),
  symbol text, base text, quote text,           -- e.g. BTCIRT / BTC / IRT
  price_tick numeric, qty_step numeric, min_notional numeric,
  listed_at timestamptz, delisted_at timestamptz,
  UNIQUE(venue_id, symbol));

-- OHLCV: one hypertable, timeframe as a column, closed bars only
CREATE TABLE ohlcv(
  symbol_id int NOT NULL, tf text NOT NULL,        -- '1m','5m','15m','1h','4h','1d'
  ts timestamptz NOT NULL,                          -- bar OPEN time, UTC
  open numeric, high numeric, low numeric, close numeric,
  volume numeric, quote_volume numeric, trades int,
  ingest_time timestamptz NOT NULL DEFAULT now(),
  is_final bool NOT NULL DEFAULT true,
  PRIMARY KEY(symbol_id, tf, ts));
SELECT create_hypertable('ohlcv','ts', chunk_time_interval => interval '7 days');

CREATE TABLE trades_raw(symbol_id int, ts timestamptz, price numeric, qty numeric, side text, trade_id text);
CREATE TABLE orderbook_snap(symbol_id int, ts timestamptz, bids jsonb, asks jsonb, depth_levels int);
CREATE TABLE derivs(symbol text, ts timestamptz, funding numeric, open_interest numeric,
                    long_short_ratio numeric, liq_long numeric, liq_short numeric);
CREATE TABLE fx_irt(ts timestamptz PRIMARY KEY, usdt_irt numeric, source text, implied_premium numeric);

-- research artifacts
CREATE TABLE features(symbol_id int, tf text, ts timestamptz, feature_set text, vals jsonb);
CREATE TABLE regimes(symbol_id int, tf text, ts timestamptz, label text, probs jsonb, method text);

-- trading
CREATE TYPE run_mode AS ENUM('backtest','paper','live');
CREATE TABLE runs(id uuid PRIMARY KEY, mode run_mode, started_at timestamptz, ended_at timestamptz,
                  code_sha text, config_hash text, data_snapshot text, seed int, notes text);
CREATE TABLE signals(id uuid PRIMARY KEY, run_id uuid, ts timestamptz, symbol_id int, tf text,
  direction text, entry numeric, stop numeric, tp jsonb, prob numeric, conf numeric,
  rr numeric, expected_value numeric, strategy text, regime text, holding_est interval,
  features_ref jsonb);
CREATE TABLE orders(id uuid PRIMARY KEY, run_id uuid, signal_id uuid, venue_id smallint,
  symbol_id int, side text, type text, qty numeric, price numeric, status text,
  client_order_id text UNIQUE, exchange_order_id text,
  created_at timestamptz, updated_at timestamptz, reject_reason text);
CREATE TABLE fills(id uuid PRIMARY KEY, order_id uuid, ts timestamptz, price numeric, qty numeric,
  fee numeric, fee_asset text, slippage_bps numeric, is_maker bool);
CREATE TABLE positions(id uuid PRIMARY KEY, run_id uuid, symbol_id int, side text,
  qty numeric, avg_entry numeric, stop numeric, opened_at timestamptz, closed_at timestamptz,
  realized_pnl_irt numeric, realized_pnl_usdt numeric, strategy text, regime text);
CREATE TABLE equity_curve(run_id uuid, ts timestamptz, balance_irt numeric, equity_irt numeric,
  equity_usdt numeric, unrealized numeric, drawdown numeric, exposure numeric);
CREATE TABLE risk_events(id bigserial, run_id uuid, ts timestamptz, kind text, detail jsonb);
```

Retention: 1m bars + raw trades compressed after 30d, order-book snapshots downsampled to per-minute L2 top-20 after 7d.

## 5. Exchange adapter interface

```python
class ExchangeAdapter(Protocol):
    venue: str
    async def get_symbols(self) -> list[SymbolInfo]: ...
    async def get_ticker(self, symbol: str) -> Ticker: ...
    async def get_orderbook(self, symbol: str, depth: int = 20) -> OrderBook: ...
    async def get_ohlcv(self, symbol: str, tf: TF, since: datetime, limit: int) -> list[Bar]: ...
    async def watch_trades(self, symbols: list[str]) -> AsyncIterator[Trade]: ...
    async def get_balance(self) -> Balances: ...
    async def get_positions(self) -> list[Position]: ...
    async def create_order(self, req: OrderRequest) -> Order: ...   # idempotent via client_order_id
    async def cancel_order(self, order_id: str, symbol: str) -> Order: ...
    async def get_order(self, order_id: str, symbol: str) -> Order: ...
    async def get_trades(self, symbol: str, since: datetime) -> list[Fill]: ...
    def fee_schedule(self) -> FeeSchedule: ...
    async def health(self) -> VenueHealth: ...   # latency p95, error rate, clock skew
```
Cross-cutting, implemented once in a wrapper, not per adapter: token-bucket rate limiting, retry with jittered exponential backoff (never retry a non-idempotent create without a client_order_id), circuit breaker, request/response audit log, clock-skew detection.

Credentials: read from env / Docker secrets only, via a `SecretsProvider`. Keys are never logged, never returned by the API, never written to the DB. `.env` is gitignored; `.env.example` ships.

## 6. API surface (FastAPI)
`/health`, `/universe`, `/scanner/top?limit=`, `/signals`, `/positions`, `/portfolio/summary`,
`/performance/{by_symbol|by_strategy|by_regime|by_tf}`, `/equity`, `/regimes/current`,
`/runs`, `/runs/{id}/report`, `/experiments`, `/risk/state`,
`POST /risk/kill-switch` (auth'd), `POST /mode` (auth'd, LIVE requires a signed approval token + all gates green), WS `/stream`.

## 7. Strategy ensemble & meta-allocator
Strategies are plugins implementing `generate(ctx) -> list[Signal]`. v1 set: `trend_follow`, `donchian_breakout`, `range_mean_reversion`, `vol_breakout`, `funding_extreme_fade`, `irt_premium_reversion`, `ml_meta`.

Allocator: regime-conditioned weights, computed **out-of-sample** — each strategy's weight comes from its rolling risk-adjusted performance *within the current regime*, shrunk toward equal-weight (a Bayesian shrinkage prior), capped per strategy, and zeroed if a strategy is in its own drawdown limit. Equal-weight is the benchmark the allocator must beat; if it doesn't, ship equal-weight.
