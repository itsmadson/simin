"""Building the right exchange for the mode, and refusing the wrong one.

This is the chokepoint. Every adapter is constructed here, which means the
mode/venue compatibility rules live in exactly one place instead of being
re-checked (and eventually forgotten) at each call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from simin.config import Settings
from simin.core.types import MarketKind, Mode
from simin.exchanges.base import Exchange, ExchangeError
from simin.exchanges.coinex import CoinExExchange
from simin.exchanges.iranian import IranianExchange
from simin.exchanges.paper import PaperExchange
from simin.exchanges.replay import synthetic_exchange
from simin.risk.dial import RiskProfile, spot_only


@dataclass(frozen=True, slots=True)
class VenueInfo:
    name: str
    display_name: str
    supports_futures: bool
    supports_shorts: bool
    max_leverage: int
    quote_asset: str
    notes_en: str
    notes_fa: str


VENUES: dict[str, VenueInfo] = {
    "paper": VenueInfo(
        "paper", "Paper (simulated)", True, True, 10, "USDT",
        "Simulated account over real prices. Cannot place a real order.",
        "حساب شبیه‌سازی‌شده روی قیمت‌های واقعی. امکان ثبت سفارش واقعی ندارد.",
    ),
    "offline": VenueInfo(
        "offline", "Offline demo", True, True, 10, "USDT",
        "Synthetic generated data — no network needed. Everything works, but no "
        "result here says anything about a real market. For learning the "
        "interface, not for judging a strategy.",
        "داده‌های ساختگی و بدون نیاز به اینترنت. همه‌چیز کار می‌کند، اما هیچ نتیجه‌ای "
        "در اینجا درباره بازار واقعی چیزی نمی‌گوید. برای یادگیری رابط کاربری، نه "
        "برای قضاوت درباره یک استراتژی.",
    ),
    "coinex": VenueInfo(
        "coinex", "CoinEx", True, True, 10, "USDT",
        "USDT perpetuals. The dial's leverage levels work here in full.",
        "قراردادهای دائمی USDT. سطوح اهرم روی این صرافی کامل کار می‌کنند.",
    ),
    "nobitex": VenueInfo(
        "nobitex", "Nobitex", False, False, 1, "IRT",
        "Spot only, Toman-denominated. Leverage is clamped to 1x and shorts "
        "are disabled. PnL is reported in both Toman and USDT, because a Toman "
        "gain during a devaluation is not a gain.",
        "فقط اسپات و بر پایه تومان. اهرم به ۱ محدود و فروش استقراضی غیرفعال است. "
        "سود و زیان هم به تومان و هم به تتر گزارش می‌شود، چون سود تومانی در دوره "
        "کاهش ارزش پول، سود نیست.",
    ),
    "wallex": VenueInfo(
        "wallex", "Wallex", False, False, 1, "IRT",
        "Spot only, Toman-denominated. Same constraints as Nobitex.",
        "فقط اسپات و بر پایه تومان. همان محدودیت‌های نوبیتکس.",
    ),
}


def venue_info(name: str) -> VenueInfo:
    info = VENUES.get(name.lower())
    if info is None:
        raise ExchangeError(f"unknown venue {name!r}; available: {sorted(VENUES)}")
    return info


def build_exchange(
    settings: Settings,
    kind: MarketKind = MarketKind.FUTURES,
    demo_speed: float = 0.0,
) -> Exchange:
    """The only place an adapter is constructed.

    In LAB mode this always returns a `PaperExchange` — wrapping a real data
    source when credentials-free public data is available, so paper prices are
    genuine while the account stays simulated.
    """
    venue = settings.venue.lower()
    info = venue_info(venue)
    creds = settings.creds(venue)

    if settings.mode is Mode.LAB:
        source: Exchange | None = None
        if venue == "offline":
            # The compressed clock only makes sense for a live runner. A
            # backtest wants the whole series at once, and gets it by asking
            # for candles before the clock has had time to matter.
            source = synthetic_exchange(
                settings.symbols or ("BTCUSDT",), seconds_per_bar=demo_speed
            )
        elif venue == "coinex":
            source = CoinExExchange(base_url=creds.base_url, kind=kind)
        elif venue in ("nobitex", "wallex"):
            source = IranianExchange(base_url=creds.base_url, venue_name=venue)
        return PaperExchange(
            data_source=source,
            starting_balance=settings.starting_equity,
            quote=info.quote_asset if source is not None else settings.quote,
        )

    # REAL mode from here down. Every one of these checks has to pass.
    problems = settings.validate_for_start()
    if problems:
        raise ExchangeError(
            "refusing to build a live exchange: " + "; ".join(problems)
        )

    if venue == "coinex":
        ex: Exchange = CoinExExchange(
            api_key=creds.key, api_secret=creds.secret, base_url=creds.base_url, kind=kind
        )
    elif venue in ("nobitex", "wallex"):
        ex = IranianExchange(
            api_key=creds.key, api_secret=creds.secret,
            base_url=creds.base_url, venue_name=venue,
        )
    else:
        raise ExchangeError(
            f"venue {venue!r} has no live adapter — REAL mode cannot run against it. "
            "The offline and paper venues are simulation only, by design."
        )

    if not ex.can_trade:
        raise ExchangeError(f"{venue} adapter reports can_trade=False; refusing REAL mode")
    return ex


def adapt_profile(profile: RiskProfile, exchange: Exchange) -> RiskProfile:
    """Reconcile the dial with what the venue can actually do.

    Called once at start. If the venue is spot-only, the profile is clamped and
    carries a visible warning — the bot never silently runs a level-9
    configuration as if the leverage had applied.
    """
    if profile.uses_leverage and not exchange.supports_futures:
        return spot_only(profile)
    if profile.max_leverage > 1:
        return profile
    return profile


def leverage_ceiling(profile: RiskProfile, exchange: Exchange) -> Decimal:
    if not exchange.supports_futures:
        return Decimal(1)
    return profile.max_leverage
