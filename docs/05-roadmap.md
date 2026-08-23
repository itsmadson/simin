# Simin — Roadmap

Each phase has a **hard exit criterion**. No phase starts before the previous one's criterion is met and demonstrated with evidence (test output, report artifact) — not asserted.

| Phase | Deliverable | Exit criterion | Est. |
|---|---|---|---|
| **0 — Repo skeleton** | monorepo, Docker Compose (postgres+timescale, redis, api, worker, frontend), `.env.example`, pre-commit (ruff/black/mypy), CI, structlog, Alembic base | `docker compose up -d` green; `pytest` + `mypy --strict` pass in CI | 1–2 d |
| **1 — Research (this doc set)** | 5 research/design docs | reviewed & approved by you | ✅ done |
| **2 — Data engine** | adapters for public global market data + Iranian public endpoints (read-only), Timescale schema, backfill CLI, WS ingestor, gap detection & repair, Parquet export, `fx_irt` premium series | 2+ years of 1h/4h/1d for the universe stored; **data-quality test suite green** (no gaps, no dup, monotonic ts, no future ts) | 1 wk |
| **3 — Feature & regime engine** | Polars feature pipeline (~50 features), point-in-time joins, regime state machine + HMM, feature store | leakage test suite passes, incl. the deliberate future-peek canary; features reproducible from a snapshot id | 1 wk |
| **4 — Backtest engine** | event-driven engine, cost model, order types, partial fills, metrics + tearsheet, MLflow logging | known-answer tests pass; a hand-computed 10-trade scenario matches to the cent; backtest vs paper replay converge | 1.5 wk |
| **5 — Baseline strategies** | the 6 rule strategies + all benchmarks from `03` §4 | full benchmark table produced across 2019–2026 walk-forward; **honest report even if everything loses** | 1 wk |
| **6 — Risk engine** | sizing, limits, vol targeting, correlation caps, circuit breakers, kill switch | every limit has a test that proves it blocks; chaos test (kill the process mid-order) leaves consistent state | 1 wk |
| **7 — ML layer** | triple-barrier labels, purged CV, LightGBM meta-labeler, vol forecaster, Optuna + DSR/PBO reporting | meta-labeler beats the raw rule OOS on DSR — **or is shipped disabled with the negative result documented** | 2 wk |
| **8 — Validation** | walk-forward runner, Monte Carlo, stress suite, gates report | `gates` report generated; go/no-go verdict rendered | 1 wk |
| **9 — Paper trading** | live-data paper engine, full trade journal, reconciliation, alerts (Telegram/webhook) | 60 days running unattended, zero unhandled exceptions | 60 d (runs in background) |
| **10 — Exchange adapters** | `ExchangeAdapter` ABC, PaperAdapter, CSVReplay, public-data adapter, plugin loader + documented example for a self-hosted local-venue adapter | adapter conformance test suite passes against a mock venue; rate-limit and retry behaviour proven | 1 wk |
| **11 — Dashboard** | Next.js, fa/en + RTL, equity/drawdown/returns charts, positions, scanner board, signal cards, regime panel, run/experiment browser, kill-switch button | all API-backed, no mock data anywhere; loads in <2s | 1.5 wk |
| **12 — Production** | hardened compose, backups, Prometheus+Grafana, runbook, deployment guide, LIVE gating with signed approval | restore-from-backup drill passes; LIVE remains disabled until gates green | 1 wk |

Parallel track: phase 9 runs in the background (60 days) while phases 10–12 proceed.

## What I need from you to start Phase 0
1. Confirm the architecture (or tell me what to change).
2. Confirm the **honest** goal framing: optimize risk-adjusted return; 200%/mo is tested as a hypothesis and will very likely be reported as unachievable.
3. Which venues you actually have accounts + API keys on (keys stay with you — never paste them here).
4. Whether the public repo ships local-venue adapters as an optional plugin (recommended) or not at all.
