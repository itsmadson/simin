# Iranian Exchange Research + Legal Risk

## ⚠️ 1. Read this before writing any adapter

On **2026-06-02** the U.S. Treasury's OFAC designated Iran's four largest crypto exchanges — **Nobitex, Wallex, Bitpin, and Ramzinex** — adding them to the SDN list under full blocking sanctions, together with several named Nobitex executives. Treasury cited Nobitex processing >50% of Iranian digital-asset inflows and facilitating IRGC-linked and ransomware-related activity; Wallex and Bitpin were cited at ~12% and ~10% of inflows.

Sources: [U.S. Treasury press release](https://home.treasury.gov/news/press-releases/sb0598) · [Chainalysis](https://www.chainalysis.com/blog/ofac-sanctions-iranian-crypto-exchanges-june-2026/) · [Elliptic](https://www.elliptic.co/insights/ofac-sanctions-nobitex-and-three-other-iranian-cryptoasset-exchanges/) · [Scorechain](https://www.scorechain.com/blog/ofac-iran-crypto-sanctions-june-2026)

**What this means for Simin, practically:**

| Area | Impact |
|---|---|
| Hosting the code | GitHub is a U.S. company. A public repo whose stated purpose is trading integration with SDN-designated entities is exposed to takedown/account risk. |
| Contributors | Any U.S. person contributing to, or operating, an integration with an SDN entity takes on real legal exposure. |
| Cloud/CI/data | US/EU cloud providers, CI runners, market-data vendors and even package registries may block or terminate. |
| Counterparty risk | Sanctioned venues face banking disruption, withdrawal freezes and elevated hack risk (Nobitex already lost ~$90M in the June 2025 breach). **Custody risk on these venues now exceeds strategy risk.** |
| Payment rails | Rial on/off-ramps of designated exchanges may be disrupted with no notice. |

**Design decisions that follow (non-negotiable in the architecture):**

1. **Core is venue-agnostic.** Simin's engine talks only to an abstract `ExchangeAdapter`. No Iranian venue name appears in the core.
2. **Shipped adapters in the public repo:** `PaperAdapter` (simulated), `CSVReplayAdapter` (backtest), and a **public-market-data-only** adapter for global venues (read-only OHLCV/funding/OI, no keys, no orders). This is what makes the OSS project useful and safe.
3. **Iranian venue adapters live as a separate, optional, self-hosted plugin package** the operator installs themselves, loaded by entry-point discovery. Repo ships the interface + a documented example, not the keys or the obligation.
4. **LIVE mode is disabled by default and gated** (see `03-risk-and-validation.md`).
5. Users are told, in the README, in both languages, that they must comply with the law applicable to them. Simin does not evade, obfuscate, or route around sanctions controls, and will not include such features.

This is not moralizing — it is threat modelling. A trading system whose venue can be cut off overnight must be built to survive that, and a repo that gets deleted helps nobody.

---

## 2. Venue comparison (as of 2026-08, from official docs + public sources)

| Exchange | Public API | WebSocket | Spot | Margin/Futures | Testnet | Maker | Taker | Rate limit | Docs | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| **Nobitex** | Yes — REST, ~66 endpoints across market data, user, spot, margin, withdrawals, API keys ([apidocs.nobitex.ir](https://apidocs.nobitex.ir/en/)) | Yes (documented WS section) | Yes | Margin trading endpoints documented | No public sandbox | ~0.1–0.25% tiered | ~0.15–0.3% tiered | ~1000 req / 10 min per IP/token; 403 on breach | Best of the Iranian venues; also a public [docs-api GitHub repo](https://github.com/nobitex/docs-api/) | Largest liquidity; **SDN-designated**; prior major breach |
| **Wallex** | Yes — REST ([api-docs.wallex.ir](https://api-docs.wallex.ir/), [developer.wallex.asia](https://developer.wallex.asia/)) | Partial/limited | Yes | No | No | tiered, `makerFeeCoefficient` exposed per fill | tiered | documented per-endpoint | Reasonable | **SDN-designated** |
| **Bitpin** | Yes — v1/v2 REST ([docs.bitpin.ir](https://docs.bitpin.ir/v1/docs/Introduction/bitpin-api-documentation)); community SDKs in Go/Python | Yes | Yes | No | No | tiered | tiered | documented | v1 endpoints flagged for deprecation — pin to v2 | **SDN-designated** |
| **Ramzinex** | Yes (REST) | Limited | Yes | No | No | ~0.2% base → 0.07% VIP | ~0.25% base | — | Thinner docs | **SDN-designated** |
| **Aban Tether / others** | varies, often OTC-style | rarely | Yes | No | No | varies | varies | — | weak | Lower liquidity; verify per-venue before use |

*Fee tiers change; Simin reads fees from a per-venue config file and, where the API exposes actual fee coefficients on fills (Wallex does), reconciles modelled cost against realized cost automatically. Never hardcode a fee.*

### Capability gaps that shape the strategy
- **No futures / limited leverage** on most Iranian venues → no shorting except via margin on Nobitex → strategies must be **long/flat**, which roughly halves the opportunity set and makes the regime filter (when to be flat) the primary source of returns.
- **No sandbox anywhere** → Simin's `PaperAdapter` isn't a nicety, it *is* the test environment. Adapter correctness must be validated against read-only endpoints + tiny-size live orders only after paper passes.
- **Thin books on alt pairs** → the universe filter in `01-research.md` §5 is load-bearing.

---

## 3. Cross-venue dispersion & arbitrage

Real, but validate the full cost chain before ever calling an opportunity valid:

```
net_edge = (price_sell_venue_B − price_buy_venue_A)
         − fee_A_taker − fee_B_taker
         − withdrawal_fee(asset) − network_time_risk
         − slippage_A(size) − slippage_B(size)
         − IRT transfer cost/latency
must exceed  min_profit_threshold  AND  survive  execution_latency
```

Honest assessment: **inter-exchange transfer arbitrage in Iran is usually a trap.** The apparent 1–2% spread is normally compensation for transfer time (10–40 min of price risk), withdrawal fees, KYC/withdrawal limits, and the risk of a venue freezing withdrawals. The variant that *can* work is **balance-parked arbitrage**: pre-fund both venues, trade the dispersion without transferring, rebalance rarely. Simin implements the detector first (measure-only mode, logging realized vs theoretical edge) and only enables execution after ≥30 days of measurement proving the edge survives.

The related and probably better play: **USDT/IRT premium mean-reversion** — the premium of the local dollar rate vs the implied global rate is a mean-reverting, locally-segmented series with a real mechanism (capital controls). Ranked as Simin's top Iran-specific hypothesis.

## 4. Smart order routing
Route per-order on: best net price after fee tier, current top-of-book depth for the order size, measured API latency (rolling p95), venue health (recent error/timeout rate), and available balance. Routing is only enabled once ≥2 adapters are live and health-checked; single-venue mode is the default.
