# Deployment & Operations Guide

## 1. Prerequisites
Docker + Docker Compose, ~4 GB RAM, ~20 GB disk for a few years of 1h/4h/1d bars across a small universe.

## 2. First run

```bash
git clone https://github.com/itsmadson/simin.git && cd simin
cp .env.example .env          # edit if needed; defaults are safe (PAPER, no keys)
docker compose up -d postgres redis
docker compose run --rm api python -m simin.cli costs      # sanity check
docker compose up -d api
open http://localhost:8000                                  # dashboard
```

`SIMIN_MODE` defaults to `paper` and `SIMIN_LIVE_APPROVAL_TOKEN` is unset, so the system cannot place a real order in this state even if you ask it to.

## 3. Load history

```bash
docker compose run --rm api python -m simin.cli backfill \
  --symbols BTCUSDT ETHUSDT SOLUSDT --timeframes 1h 4h 1d --start 2021-01-01
```

Backfill is idempotent and resumable — safe to re-run after any interruption. It validates as it goes and writes a quality report to `data_quality_log`; unrepaired gaps are errors, not warnings, because features computed across a gap are silently wrong.

## 4. Run research

```bash
docker compose run --rm api python -m simin.cli research \
  --symbol BTCUSDT --symbol-id 1 --timeframe 4h \
  --strategy trend_follow --start 2021-01-01 --trials 1
```

Set `--trials` to the number of parameter variants you actually tried. It feeds the deflated Sharpe ratio, and understating it inflates your own result — the person that deceives is you.

Exit code is `0` only when every Go/No-Go gate passes, so this is CI-friendly.

## 5. Paper trading

```bash
docker compose --profile trading up -d trader
docker compose logs -f trader
```

Runs the same strategy code as the backtester against live data with simulated money. Leave it for **at least 60 days and 200 closed trades** before considering anything else (gates 9 and 10).

The trader is a **single-replica service on purpose**. Scaling it to 2 replicas opens two copies of every position. Do not.

## 6. Going live (the long version)

1. `simin research` → all 12 gates green.
2. 60+ days of paper with zero unhandled exceptions and slippage ≤1.5× modelled.
3. Write the operator-supplied venue adapter as a plugin (see `docs/04-exchanges-iran.md` §1 for why it is not in this repo), implementing `ExchangeAdapter` and passing the conformance tests.
4. Issue `SIMIN_LIVE_APPROVAL_TOKEN`, set `SIMIN_MODE=live`.
5. Fund **2% of intended capital**. Not more. `initial_live_allocation()` enforces it.
6. Watch for 30 days. Any red gate reverts to paper automatically.

If step 1 never goes green, that is a result. Most strategies never get there, and the ones that ship anyway are the ones that lose money.

## 7. Operations

| Task | Command |
|---|---|
| Health | `curl localhost:8000/health` |
| Kill switch | `curl -XPOST localhost:8000/kill-switch?reason=manual` |
| Data quality | `simin check --symbol BTCUSDT --symbol-id 1 --start 2024-01-01` |
| Cost floor | `simin costs` |
| Target reality check | `simin target --monthly-pct 200` |
| Backup | `docker compose exec postgres pg_dump -U simin simin \| gzip > backup.sql.gz` |
| Restore drill | restore into a scratch database and run `simin check` against it — an untested backup is not a backup |

**The kill switch latches.** There is no resume endpoint by design: restarting a halted system is a human decision made after understanding why it halted.

## 8. Monitoring

Watch these four, in priority order:

1. **Reconciliation mismatches** — the system's view of positions vs the venue's. Any mismatch is a stop-everything event.
2. **Realized vs modelled slippage** — if realized exceeds 1.5× modelled, the cost model is wrong and every backtest built on it is optimistic.
3. **Data staleness** — trading on a stale feed is worse than not trading.
4. **Drawdown vs Monte Carlo p95** — exceeding it means live behaviour has left the distribution the strategy was validated in.

## 9. Security

- Credentials only via environment or Docker secrets. `.env` is gitignored; secrets never enter the database, logs, or API responses (the logger redacts by key name).
- Postgres and Redis ports are exposed in the default compose file for convenience on a single box — **remove those port mappings before putting this on a network you don't control.**
- The API has no authentication. Bind it to localhost or put it behind a reverse proxy with auth before exposing it.
