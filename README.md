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
docker compose up -d
```

That is the whole setup. It starts Postgres and Redis, creates the schema, downloads real
market history, launches the API and dashboard on <http://localhost:8000>, and starts paper
trading against live data. No `.env` is required — every setting has a working default, and
paper mode cannot place a real order.

Useful overrides:

```bash
SIMIN_API_PORT=8010 docker compose up -d              # port 8000 already taken
SIMIN_BOOTSTRAP_SYMBOLS="BTCUSDT ETHUSDT" \
SIMIN_BOOTSTRAP_START=2020-01-01 docker compose up -d  # different history
SIMIN_PG_IMAGE=timescale/timescaledb:latest-pg16 docker compose up -d   # hypertables
docker compose build --build-arg PIP_INDEX_URL=<mirror>  # if pypi.org is unreachable
```

TimescaleDB is used when the image provides it and skipped when it does not; the schema and
results are identical either way.

Then run the research pipeline:

```bash
docker compose exec api python -m simin.cli research \
  --symbol BTCUSDT --symbol-id 1 --timeframe 4h --strategy trend_follow --start 2022-01-01
```

It prints in-sample, out-of-sample and 2×-cost results, every benchmark, every walk-forward
window, a Monte Carlo distribution, and the 12-gate verdict — and exits non-zero unless every
gate passes.

## The app

`docker compose up -d`, then <http://localhost:8000>.

**LAB vs REAL.** The mode badge is always visible in the header. LAB covers backtest and paper
trading; REAL is locked and cannot be unlocked from the UI — see the Go Live page for the
twelve gates and the Wallet page for what connecting real funds actually requires.

| Page | What it is for |
|---|---|
| **Overview** | equity, drawdown, realized/unrealized PnL, fees, equity curve, live activity feed |
| **Positions** | open and closed tabs, entry, stop, strategy, regime, realized PnL |
| **Signals & Orders** | every signal a strategy produced and every order that followed, including rejections and why |
| **Market** | current regime per symbol with the features behind it, and whether trading is permitted |
| **Performance** | PnL by strategy, by symbol, by regime |
| **Lab** | run a backtest from the browser: pick symbol, timeframe, strategy, risk profile, 1× or 2× cost, regime filter on/off. Returns the full metric set and every benchmark side by side. Read-only — the Lab cannot open a position |
| **Data** | stored history coverage per symbol and timeframe |
| **Wallet** | set the paper balance; see venue costs; see exactly what real-money connection requires |
| **Go Live** | the 12 gates with live evidence, plus the target reality check |
| **Settings** | risk profile, active risk limits, data source |

**On connecting real money.** The Wallet page shows the status of a real venue connection but
does not accept credentials, by design. Keys arrive through the environment or a Docker
secret, never through a web form and never into the database — an endpoint that accepts a
withdrawal-capable key is a liability, not a feature. The repository also ships no adapter for
any sanctioned venue (see [`docs/04`](docs/04-exchanges-iran.md)); that adapter is an
operator-installed plugin.

## Real results (not a simulation of results)

Binance spot, 2022-01-01 → 2026-08-23, 4h bars, aggressive risk profile, Iranian-venue cost
model (~1.1% round trip). 152,000+ real bars loaded through the pipeline.

| Symbol | Strategy | Trades | Return | Sharpe | MaxDD | Fees as % of gross |
|---|---|---:|---:|---:|---:|---:|
| BTC | buy_and_hold | — | **+62.6%** | 0.46 | −67.2% | 0% |
| BTC | donchian_breakout | 21 | +7.0% | 0.41 | −6.1% | 19% |
| BTC | trend_follow | 93 | −9.5% | −0.34 | −14.9% | 31% |
| BTC | rsi_oversold | 19 | −12.5% | −1.23 | −12.5% | **159%** |
| BTC | range_mean_reversion | 101 | −24.3% | −1.69 | −25.1% | 73% |
| ETH | buy_and_hold | — | −34.8% | 0.19 | −76.2% | 0% |
| ETH | best strategy (donchian) | 20 | −2.0% | −0.12 | −6.5% | 27% |
| SOL | buy_and_hold | — | −45.4% | 0.33 | −95.1% | 0% |
| SOL | best strategy (vol_breakout) | 2 | +3.0% | 0.93 | −0.6% | 2% |

**What this says, plainly:**

1. **No strategy here beat buying and holding BTC.** The best one made 7% while BTC made 62%.
2. **Fees ate everything.** Textbook RSI paid 159% of its gross profit in fees — it made money
   before costs and lost money after. That is the entire story of most retail bots.
3. **The strategies did protect capital.** Buy-and-hold survived a −67% (BTC), −76% (ETH) and
   −95% (SOL) drawdown. The systematic strategies stayed under −25%. Losing less is worth more
   than it sounds.
4. `simin research` on BTC 4h returns **NO-GO**: 38% walk-forward consistency, deflated Sharpe
   0.14, negative at 2× cost. The system correctly refuses to green-light itself.

Nothing here is tuned. That is deliberate — these are the honest baseline numbers a real edge
would have to beat, and publishing them first is what makes any later improvement believable.

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
| Dashboard | 10-page app: sidebar nav, Overview, Positions, Signals & Orders, Market, Performance, Lab, Data, Wallet, Go Live, Settings — bilingual fa/en with full RTL, no build step, no external requests |
| Session records | Every signal, order, fill, position, equity mark and risk event is written to Postgres, so the UI shows what happened rather than a reconstruction |

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
Python 3.12+ · FastAPI · NumPy · PostgreSQL (TimescaleDB optional) · Redis · Docker Compose. Optional research extras: LightGBM, scikit-learn, Optuna, Polars. The dashboard is a self-contained page with no build step and no external requests.

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
