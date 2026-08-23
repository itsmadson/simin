# Simin · سیمین

**An open-source, research-first adaptive crypto trading platform.**
Backtest → Paper → (gated) Live. Persian & English. Honest about what works.

> **Status: phases 0–11 implemented.** Data engine, feature/regime engine, event-driven backtester, risk engine, meta-labeling ML, walk-forward + Monte Carlo, Go/No-Go gates, paper trading, dashboard. 208 tests. Live trading is disabled and gated.

---

## Read this first (the honest part)

- **Simin will not make you rich quickly.** A target of +200%/month compounds to 531,441× per year. Nobody has ever done that sustainably. Chasing it requires leverage that guarantees eventual ruin. Simin treats that number as a hypothesis to test and expects to report it as unachievable. See [`docs/01-research.md` §0.3](docs/01-research.md).
- **Most retail bots lose to fees and spread, not to bad indicators.** Round-trip cost on Iranian venues is ~0.5–1.2%. Every strategy is judged net of that, and again at 2× that.
- **A Toman-denominated profit may be pure Rial devaluation, not trading skill.** Simin reports PnL in **both IRT and USDT** and benchmarks against simply holding USDT.
- **If the ML adds nothing, it ships disabled** and the README says so. No fake AI.
- **Live trading is disabled by default** behind 12 automated gates plus human approval.

## ⚠️ Legal / operational notice

On 2026-06-02, OFAC designated Nobitex, Wallex, Bitpin and Ramzinex to the SDN list ([U.S. Treasury](https://home.treasury.gov/news/press-releases/sb0598)). Consequences for this project are analysed in [`docs/04-exchanges-iran.md`](docs/04-exchanges-iran.md).

This repository ships **no credentials and no adapters to designated venues**. The core is venue-agnostic; it includes a paper adapter, a backtest replay adapter, and read-only public market-data adapters. Anyone operating this software is responsible for complying with the laws that apply to them. Simin contains no functionality intended to evade sanctions or compliance controls, and pull requests adding such functionality will be rejected.

## Quick start

```bash
cp .env.example .env
docker compose up -d postgres redis api
open http://localhost:8000                       # bilingual dashboard (fa/en, RTL)

# what a target actually costs, computed from the live cost model
docker compose run --rm api python -m simin.cli target --monthly-pct 200

# load history, then run the full honest pipeline
docker compose run --rm api python -m simin.cli backfill --symbols BTCUSDT --start 2021-01-01
docker compose run --rm api python -m simin.cli research --symbol BTCUSDT --symbol-id 1 \
    --timeframe 4h --strategy trend_follow --start 2021-01-01
```

`simin research` prints in-sample, out-of-sample and 2×-cost results, every benchmark, every
walk-forward window, a Monte Carlo distribution, and the 12-gate verdict. It exits non-zero
unless every gate passes.

## What is built

| Area | Status |
|---|---|
| Domain types, data quality, backfill | Timescale schema, idempotent resumable backfill, gap/staleness detection |
| Feature + regime engine | ~20 causal features, strict as-of multi-timeframe join, regime state machine + playbook |
| Backtester | event-driven, T+1 fills, spread + sqrt impact + latency, stop-before-target, depth-capped sizing |
| Risk engine | vol-scaled sizing, drawdown throttles, correlated-beta cap, venue cap, latching circuit breakers |
| Strategies | 4 strategies + 4 benchmarks incl. random-entry (isolates signal from sizing) |
| ML | triple-barrier labels, purged/embargoed CV, PBO, logistic baseline + LightGBM, calibration |
| Validation | walk-forward, Monte Carlo, deflated Sharpe, 12 Go/No-Go gates |
| Paper trading | paper adapter with idempotent orders + partial fills, single-leader trader loop |
| Dashboard | FastAPI + self-contained bilingual UI, kill switch, live target reality check |

## Documentation

| Doc | Contents |
|---|---|
| [01 — Deep Research](docs/01-research.md) | indicator ranking after costs, ML benchmark verdict, regime detection, timeframes, feature set, cost model |
| [02 — Architecture](docs/02-architecture.md) | components, tech stack rationale, DB schema, adapter interface, API surface |
| [03 — Risk & Validation](docs/03-risk-and-validation.md) | risk engine, backtest anti-bias rules, walk-forward, Monte Carlo, **Go/No-Go gates** |
| [04 — Iranian Exchanges](docs/04-exchanges-iran.md) | API/WS/fee/limit comparison table, sanctions impact, arbitrage reality check |
| [05 — Roadmap](docs/05-roadmap.md) | 12 phases with hard exit criteria |
| [06 — Deployment](docs/06-deployment.md) | run it, operate it, and what going live actually requires |

## Planned stack
Python 3.12 · FastAPI · Polars · PostgreSQL + TimescaleDB · Redis · Arq · LightGBM · Optuna · MLflow · Next.js + TypeScript + Tailwind + TradingView Lightweight Charts · Docker Compose.

## License
MIT

---

<div dir="rtl">

# سیمین

**پلتفرم متن‌باز معاملات الگوریتمی ارز دیجیتال، با اولویت پژوهش.**
بک‌تست ← معامله کاغذی ← معامله واقعی (فقط پس از عبور از دروازه‌های سخت‌گیرانه). دوزبانه فارسی/انگلیسی.

> **وضعیت: فاز ۱ — پژوهش و معماری.** هنوز کد معاملاتی نوشته نشده است.

## بخش صادقانه

- **سیمین شما را سریع ثروتمند نمی‌کند.** هدف ۲۰۰٪ در ماه یعنی ۵۳۱٬۴۴۱ برابر شدن سرمایه در یک سال. چنین چیزی هرگز به‌صورت پایدار محقق نشده و رسیدن به آن نیازمند اهرمی است که ورشکستگی را قطعی می‌کند. این عدد به‌عنوان یک فرضیه آزمایش می‌شود و به احتمال بسیار زیاد «غیرقابل دستیابی» گزارش خواهد شد.
- **بیشتر ربات‌های معاملاتی به‌خاطر کارمزد و اسپرد شکست می‌خورند، نه اندیکاتور بد.** هزینه رفت‌وبرگشت در صرافی‌های ایرانی حدود ۰٫۵ تا ۱٫۲ درصد است. هر استراتژی پس از کسر این هزینه و همچنین در حالت دو برابر هزینه ارزیابی می‌شود.
- **سود تومانی ممکن است صرفاً کاهش ارزش ریال باشد، نه مهارت معاملاتی.** سیمین سود و زیان را هم به تومان و هم به تتر گزارش می‌کند و آن را با «صرفاً نگه‌داشتن تتر» مقایسه می‌کند.
- **اگر هوش مصنوعی ارزش افزوده‌ای نداشته باشد، غیرفعال ارائه می‌شود** و همین در مستندات نوشته می‌شود. هوش مصنوعی تزئینی نداریم.
- **حالت معامله واقعی به‌صورت پیش‌فرض غیرفعال است** و پشت ۱۲ دروازه خودکار به‌علاوه تأیید انسانی قرار دارد.

## هشدار حقوقی

در تاریخ ۱۲ خرداد ۱۴۰۵ (۲۰۲۶-۰۶-۰۲) دفتر OFAC آمریکا صرافی‌های نوبیتکس، والکس، بیت‌پین و رمزینکس را در فهرست SDN قرار داد. تحلیل پیامدهای فنی و عملیاتی آن در `docs/04-exchanges-iran.md` آمده است. این مخزن هیچ کلید API و هیچ آداپتوری برای این صرافی‌ها منتشر نمی‌کند؛ هسته سیستم مستقل از صرافی است. مسئولیت رعایت قوانین بر عهده کاربر است.

</div>
