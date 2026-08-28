"""The risk dial: one number from 1 to 10 that configures the entire bot.

This is the product. Everything else exists to serve it.

The user picks a level, and that level deterministically fixes every risk
parameter in the system: how much of the account is risked per trade, how much
leverage is allowed, how tight the stops are, how many positions may be open at
once, how good a setup has to look before it is taken, and the loss limits at
which the bot stops trading.

## About the return targets

Each level carries a `target_monthly_return`. That number is a *design target*,
not a promise, and the code never treats it as an expectation. A level's honest
number is `empirical`, which is filled in by the calibration job in
`simin.lab.calibrate` from actual walk-forward backtests on real history.

The arithmetic that makes this necessary: +200%/month compounds to ~531,000x per
year. No fund, desk, or individual has ever sustained that. Levels 8-10 exist
because the user asked for a full 1-10 range, and they are configured to be
genuinely aggressive -- but their honest description is "high variance, wide
outcome distribution, meaningful probability of ruin", and `ruin_probability`
below is estimated from Monte Carlo, not invented.

The UI is required to show `empirical` next to `target_monthly_return` whenever
calibration data exists. `RiskProfile.headline()` enforces that pairing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Final

from simin.core.types import TF

MIN_LEVEL: Final = 1
MAX_LEVEL: Final = 10


@dataclass(frozen=True, slots=True)
class EmpiricalProfile:
    """What a level actually did in walk-forward testing. Written by calibration."""

    monthly_return_median: float
    monthly_return_p05: float
    monthly_return_p95: float
    max_drawdown_median: float
    max_drawdown_p95: float
    win_rate: float
    profit_factor: float
    sharpe: float
    trades_per_month: float
    ruin_probability: float
    sample_months: int
    calibrated_at: str
    symbol_scope: str = ""

    @property
    def is_profitable(self) -> bool:
        return self.monthly_return_median > 0 and self.profit_factor > 1.0


@dataclass(frozen=True, slots=True)
class RiskProfile:
    """Every risk knob in the system, fixed by one integer."""

    level: int
    name_en: str
    name_fa: str
    description_en: str
    description_fa: str

    # --- Sizing -------------------------------------------------------------
    #: Fraction of equity lost if a trade hits its stop. The single most
    #: important number in the whole system.
    risk_per_trade: Decimal
    #: Hard cap on leverage. The dial may use less; it may never use more.
    max_leverage: Decimal
    #: Cap on total notional across all positions, as a multiple of equity.
    max_gross_exposure: Decimal
    max_concurrent_positions: int
    #: Ceiling on any single position's notional, as a fraction of equity*lev.
    max_position_notional_pct: Decimal

    # --- Entry selectivity --------------------------------------------------
    #: Minimum confluence score (0..1) a setup needs. Low risk = picky.
    min_confluence: float
    #: Timeframes this level trades. First is the signal TF, rest are context.
    signal_tf: TF
    context_tf: TF
    allow_shorts: bool
    allow_counter_trend: bool
    #: Trades to allow per day before the level refuses more. Overtrading is
    #: how a good edge is converted into fees.
    max_trades_per_day: int
    #: Bars that must pass after an exit before re-entering the same symbol.
    cooldown_bars: int

    # --- Exits --------------------------------------------------------------
    #: Stop distance as a multiple of ATR. Tighter = more leverage survivable
    #: but more stop-outs on noise.
    atr_stop_mult: Decimal
    #: First target, in R.
    take_profit_r: Decimal
    #: Move stop to breakeven once price reaches this many R. 0 disables.
    breakeven_at_r: Decimal
    #: Trail the stop at this ATR multiple once breakeven is armed. 0 disables.
    trail_atr_mult: Decimal
    #: Scale out this fraction of the position at take_profit_r, letting the
    #: rest run on the trail. 0 = all-or-nothing.
    partial_exit_pct: Decimal
    #: Force-close a position that has gone nowhere after this many bars.
    time_stop_bars: int

    # --- Circuit breakers ---------------------------------------------------
    #: Realised loss in one UTC day, as a fraction of the day's starting
    #: equity, that halts trading until the next day.
    daily_loss_halt: Decimal
    #: Peak-to-trough drawdown that halts the bot entirely, pending a human.
    max_drawdown_halt: Decimal
    #: Consecutive losers that trigger a cooling-off period.
    loss_streak_halt: int
    #: Fraction of risk_per_trade to use while recovering from a halt.
    recovery_risk_factor: Decimal

    # --- Declared targets and measured reality ------------------------------
    target_monthly_return: float
    empirical: EmpiricalProfile | None = None
    warnings_en: tuple[str, ...] = ()
    warnings_fa: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not MIN_LEVEL <= self.level <= MAX_LEVEL:
            raise ValueError(f"risk level {self.level} outside 1..10")
        if self.risk_per_trade <= 0 or self.risk_per_trade > Decimal("0.10"):
            raise ValueError("risk_per_trade must be in (0, 0.10]")
        if self.max_leverage < 1:
            raise ValueError("max_leverage must be >= 1")

    # --- Derived ------------------------------------------------------------

    @property
    def uses_leverage(self) -> bool:
        return self.max_leverage > 1

    @property
    def is_aggressive(self) -> bool:
        return self.level >= 8

    @property
    def kind(self) -> str:
        return "futures" if self.uses_leverage else "spot"

    @property
    def worst_case_day(self) -> Decimal:
        """Loss if every concurrent position stops out at once. Correlated
        crypto makes this the realistic bad day, not a tail."""
        return min(
            self.risk_per_trade * self.max_concurrent_positions,
            self.daily_loss_halt,
        )

    def headline(self) -> dict[str, object]:
        """What the UI shows for this level. Target and reality, side by side."""
        return {
            "level": self.level,
            "name_en": self.name_en,
            "name_fa": self.name_fa,
            "target_monthly_return": self.target_monthly_return,
            "measured_monthly_return": (
                self.empirical.monthly_return_median if self.empirical else None
            ),
            "measured_max_drawdown": (
                self.empirical.max_drawdown_median if self.empirical else None
            ),
            "ruin_probability": self.empirical.ruin_probability if self.empirical else None,
            "calibrated": self.empirical is not None,
            "leverage": float(self.max_leverage),
            "risk_per_trade": float(self.risk_per_trade),
            "worst_case_day": float(self.worst_case_day),
            "kind": self.kind,
            "warnings_en": list(self.warnings_en),
            "warnings_fa": list(self.warnings_fa),
        }

    def with_empirical(self, e: EmpiricalProfile) -> RiskProfile:
        return replace(self, empirical=e)

    def scaled(self, factor: Decimal) -> RiskProfile:
        """A temporarily de-risked copy, used while recovering from a halt."""
        return replace(
            self,
            risk_per_trade=self.risk_per_trade * factor,
            max_concurrent_positions=max(1, int(self.max_concurrent_positions * float(factor))),
            max_gross_exposure=self.max_gross_exposure * factor,
        )


def _d(x: str) -> Decimal:
    return Decimal(x)


#: The dial. Every parameter below was chosen so that the *shape* of the curve
#: is right: risk-per-trade and leverage rise geometrically, selectivity and
#: stop width fall, and the circuit breakers widen just enough to let the
#: aggressive levels breathe without removing them entirely.
_LEVELS: Final[tuple[RiskProfile, ...]] = (
    RiskProfile(
        level=1,
        name_en="Vault",
        name_fa="خزانه",
        description_en="Capital preservation. Trades only the cleanest 4h setups, spot only.",
        description_fa="حفظ سرمایه. فقط تمیزترین موقعیت‌های ۴ ساعته، فقط اسپات.",
        risk_per_trade=_d("0.0025"),
        max_leverage=_d("1"),
        max_gross_exposure=_d("0.30"),
        max_concurrent_positions=1,
        max_position_notional_pct=_d("0.30"),
        min_confluence=0.80,
        signal_tf=TF.H4,
        context_tf=TF.D1,
        allow_shorts=False,
        allow_counter_trend=False,
        max_trades_per_day=1,
        cooldown_bars=6,
        atr_stop_mult=_d("3.0"),
        take_profit_r=_d("3.0"),
        breakeven_at_r=_d("1.0"),
        trail_atr_mult=_d("3.0"),
        partial_exit_pct=_d("0.5"),
        time_stop_bars=30,
        daily_loss_halt=_d("0.010"),
        max_drawdown_halt=_d("0.06"),
        loss_streak_halt=3,
        recovery_risk_factor=_d("0.5"),
        target_monthly_return=0.02,
    ),
    RiskProfile(
        level=2,
        name_en="Careful",
        name_fa="محتاط",
        description_en="Slow trend-following on 4h. Long only, wide stops, few trades.",
        description_fa="دنبال‌کردن آرام روند در ۴ ساعته. فقط خرید، حد ضرر عریض، معاملات کم.",
        risk_per_trade=_d("0.005"),
        max_leverage=_d("1"),
        max_gross_exposure=_d("0.50"),
        max_concurrent_positions=2,
        max_position_notional_pct=_d("0.35"),
        min_confluence=0.72,
        signal_tf=TF.H4,
        context_tf=TF.D1,
        allow_shorts=False,
        allow_counter_trend=False,
        max_trades_per_day=2,
        cooldown_bars=4,
        atr_stop_mult=_d("2.8"),
        take_profit_r=_d("2.8"),
        breakeven_at_r=_d("1.0"),
        trail_atr_mult=_d("2.8"),
        partial_exit_pct=_d("0.5"),
        time_stop_bars=28,
        daily_loss_halt=_d("0.015"),
        max_drawdown_halt=_d("0.08"),
        loss_streak_halt=3,
        recovery_risk_factor=_d("0.5"),
        target_monthly_return=0.04,
    ),
    RiskProfile(
        level=3,
        name_en="Steady",
        name_fa="پیوسته",
        description_en="Two-hour swings, both directions, still spot. The first level that shorts.",
        description_fa="نوسان‌گیری دوساعته در هر دو جهت، همچنان اسپات. اولین سطحی که فروش استقراضی دارد.",
        risk_per_trade=_d("0.0075"),
        max_leverage=_d("1"),
        max_gross_exposure=_d("0.70"),
        max_concurrent_positions=2,
        max_position_notional_pct=_d("0.40"),
        min_confluence=0.66,
        signal_tf=TF.H2,
        context_tf=TF.H12,
        allow_shorts=True,
        allow_counter_trend=False,
        max_trades_per_day=3,
        cooldown_bars=3,
        atr_stop_mult=_d("2.5"),
        take_profit_r=_d("2.5"),
        breakeven_at_r=_d("1.0"),
        trail_atr_mult=_d("2.5"),
        partial_exit_pct=_d("0.5"),
        time_stop_bars=24,
        daily_loss_halt=_d("0.020"),
        max_drawdown_halt=_d("0.10"),
        loss_streak_halt=4,
        recovery_risk_factor=_d("0.5"),
        target_monthly_return=0.07,
    ),
    RiskProfile(
        level=4,
        name_en="Balanced",
        name_fa="متعادل",
        description_en="The default. 2h signals, 2x leverage, oscillation plus trend pullbacks.",
        description_fa="پیش‌فرض. سیگنال دوساعته، اهرم ۲، نوسان‌گیری به‌همراه اصلاح روند.",
        risk_per_trade=_d("0.010"),
        max_leverage=_d("2"),
        max_gross_exposure=_d("1.20"),
        max_concurrent_positions=3,
        max_position_notional_pct=_d("0.45"),
        min_confluence=0.60,
        signal_tf=TF.H2,
        context_tf=TF.H12,
        allow_shorts=True,
        allow_counter_trend=False,
        max_trades_per_day=4,
        cooldown_bars=2,
        atr_stop_mult=_d("2.2"),
        take_profit_r=_d("2.2"),
        breakeven_at_r=_d("0.9"),
        trail_atr_mult=_d("2.2"),
        partial_exit_pct=_d("0.5"),
        time_stop_bars=22,
        daily_loss_halt=_d("0.030"),
        max_drawdown_halt=_d("0.15"),
        loss_streak_halt=4,
        recovery_risk_factor=_d("0.5"),
        target_monthly_return=0.12,
        warnings_en=("Leverage begins here. A 50% adverse move on 2x liquidates.",),
        warnings_fa=("اهرم از اینجا شروع می‌شود. حرکت ۵۰٪ خلاف جهت با اهرم ۲ لیکویید می‌کند.",),
    ),
    RiskProfile(
        level=5,
        name_en="Active",
        name_fa="فعال",
        description_en="Hourly candles, 3x, counter-trend mean reversion enabled.",
        description_fa="کندل ساعتی، اهرم ۳، بازگشت به میانگین خلاف روند فعال.",
        risk_per_trade=_d("0.015"),
        max_leverage=_d("3"),
        max_gross_exposure=_d("1.80"),
        max_concurrent_positions=3,
        max_position_notional_pct=_d("0.50"),
        min_confluence=0.55,
        signal_tf=TF.H1,
        context_tf=TF.H6,
        allow_shorts=True,
        allow_counter_trend=True,
        max_trades_per_day=6,
        cooldown_bars=2,
        atr_stop_mult=_d("2.0"),
        take_profit_r=_d("2.0"),
        breakeven_at_r=_d("0.8"),
        trail_atr_mult=_d("2.0"),
        partial_exit_pct=_d("0.5"),
        time_stop_bars=20,
        daily_loss_halt=_d("0.040"),
        max_drawdown_halt=_d("0.20"),
        loss_streak_halt=5,
        recovery_risk_factor=_d("0.5"),
        target_monthly_return=0.20,
        warnings_en=("3x leverage: a 33% adverse move is a total loss of margin.",),
        warnings_fa=("اهرم ۳: حرکت ۳۳٪ خلاف جهت یعنی از دست رفتن کل مارجین.",),
    ),
    RiskProfile(
        level=6,
        name_en="Assertive",
        name_fa="جسور",
        description_en="Hourly, 4x, looser entry filter. Trade count roughly doubles.",
        description_fa="ساعتی، اهرم ۴، فیلتر ورود بازتر. تعداد معاملات تقریباً دو برابر.",
        risk_per_trade=_d("0.020"),
        max_leverage=_d("4"),
        max_gross_exposure=_d("2.60"),
        max_concurrent_positions=4,
        max_position_notional_pct=_d("0.55"),
        min_confluence=0.50,
        signal_tf=TF.H1,
        context_tf=TF.H6,
        allow_shorts=True,
        allow_counter_trend=True,
        max_trades_per_day=8,
        cooldown_bars=1,
        atr_stop_mult=_d("1.8"),
        take_profit_r=_d("1.9"),
        breakeven_at_r=_d("0.7"),
        trail_atr_mult=_d("1.8"),
        partial_exit_pct=_d("0.5"),
        time_stop_bars=18,
        daily_loss_halt=_d("0.055"),
        max_drawdown_halt=_d("0.25"),
        loss_streak_halt=5,
        recovery_risk_factor=_d("0.4"),
        target_monthly_return=0.30,
        warnings_en=("At 2% risk per trade, five consecutive losses cost ~10% of the account.",),
        warnings_fa=("با ریسک ۲٪ در هر معامله، پنج ضرر پشت‌سرهم حدود ۱۰٪ حساب را می‌برد.",),
    ),
    RiskProfile(
        level=7,
        name_en="Aggressive",
        name_fa="تهاجمی",
        description_en="15-minute candles, 5x. Scalping the oscillation, many small trades.",
        description_fa="کندل ۱۵ دقیقه، اهرم ۵. اسکالپ نوسان، معاملات کوچک و پرتعداد.",
        risk_per_trade=_d("0.025"),
        max_leverage=_d("5"),
        max_gross_exposure=_d("3.50"),
        max_concurrent_positions=4,
        max_position_notional_pct=_d("0.60"),
        min_confluence=0.46,
        signal_tf=TF.M15,
        context_tf=TF.H4,
        allow_shorts=True,
        allow_counter_trend=True,
        max_trades_per_day=14,
        cooldown_bars=1,
        atr_stop_mult=_d("1.6"),
        take_profit_r=_d("1.7"),
        breakeven_at_r=_d("0.6"),
        trail_atr_mult=_d("1.6"),
        partial_exit_pct=_d("0.6"),
        time_stop_bars=16,
        daily_loss_halt=_d("0.070"),
        max_drawdown_halt=_d("0.30"),
        loss_streak_halt=6,
        recovery_risk_factor=_d("0.4"),
        target_monthly_return=0.50,
        warnings_en=(
            "15m trading pays fees ~10x more often than 4h. The edge must survive that.",
            "5x leverage liquidates on a 20% adverse move.",
        ),
        warnings_fa=(
            "معامله در ۱۵ دقیقه حدود ۱۰ برابر بیشتر از ۴ ساعته کارمزد می‌دهد. سود باید از آن جان سالم ببرد.",
            "اهرم ۵ با حرکت ۲۰٪ خلاف جهت لیکویید می‌شود.",
        ),
    ),
    RiskProfile(
        level=8,
        name_en="High Voltage",
        name_fa="پرفشار",
        description_en="15-minute, 6x, weak filters. Wide outcome distribution.",
        description_fa="۱۵ دقیقه، اهرم ۶، فیلترهای ضعیف. پراکندگی نتایج بسیار زیاد.",
        risk_per_trade=_d("0.030"),
        max_leverage=_d("6"),
        max_gross_exposure=_d("4.50"),
        max_concurrent_positions=5,
        max_position_notional_pct=_d("0.65"),
        min_confluence=0.42,
        signal_tf=TF.M15,
        context_tf=TF.H4,
        allow_shorts=True,
        allow_counter_trend=True,
        max_trades_per_day=20,
        cooldown_bars=0,
        atr_stop_mult=_d("1.4"),
        take_profit_r=_d("1.6"),
        breakeven_at_r=_d("0.5"),
        trail_atr_mult=_d("1.4"),
        partial_exit_pct=_d("0.6"),
        time_stop_bars=14,
        daily_loss_halt=_d("0.090"),
        max_drawdown_halt=_d("0.35"),
        loss_streak_halt=6,
        recovery_risk_factor=_d("0.35"),
        target_monthly_return=0.80,
        warnings_en=(
            "Levels 8-10 have a materially non-zero probability of losing most of the account.",
            "Only fund these with money you can lose entirely.",
        ),
        warnings_fa=(
            "سطح ۸ تا ۱۰ احتمال قابل‌توجهی برای از دست دادن بخش بزرگی از حساب دارد.",
            "فقط با پولی وارد شوید که توان از دست دادن کاملش را دارید.",
        ),
    ),
    RiskProfile(
        level=9,
        name_en="Reckless",
        name_fa="بی‌پروا",
        description_en="5-minute candles, 8x. Near-continuous exposure, minimal filtering.",
        description_fa="کندل ۵ دقیقه، اهرم ۸. تقریباً همیشه در بازار، کمترین فیلتر.",
        risk_per_trade=_d("0.040"),
        max_leverage=_d("8"),
        max_gross_exposure=_d("6.00"),
        max_concurrent_positions=5,
        max_position_notional_pct=_d("0.70"),
        min_confluence=0.38,
        signal_tf=TF.M5,
        context_tf=TF.H1,
        allow_shorts=True,
        allow_counter_trend=True,
        max_trades_per_day=30,
        cooldown_bars=0,
        atr_stop_mult=_d("1.2"),
        take_profit_r=_d("1.5"),
        breakeven_at_r=_d("0.4"),
        trail_atr_mult=_d("1.2"),
        partial_exit_pct=_d("0.6"),
        time_stop_bars=12,
        daily_loss_halt=_d("0.120"),
        max_drawdown_halt=_d("0.45"),
        loss_streak_halt=7,
        recovery_risk_factor=_d("0.30"),
        target_monthly_return=1.30,
        warnings_en=(
            "8x leverage liquidates on a 12.5% adverse move. Crypto does that in an hour.",
            "At 4% risk per trade, ten consecutive losses cost ~34% of the account.",
        ),
        warnings_fa=(
            "اهرم ۸ با حرکت ۱۲.۵٪ خلاف جهت لیکویید می‌شود. کریپتو این کار را در یک ساعت می‌کند.",
            "با ریسک ۴٪، ده ضرر پشت‌سرهم حدود ۳۴٪ حساب را می‌برد.",
        ),
    ),
    RiskProfile(
        level=10,
        name_en="Ruin Or Riches",
        name_fa="بُرد یا باخت",
        description_en="5-minute, 10x, almost no filter. Built because you asked for a 10.",
        description_fa="۵ دقیقه، اهرم ۱۰، تقریباً بدون فیلتر. ساخته شده چون سطح ۱۰ خواستید.",
        risk_per_trade=_d("0.050"),
        max_leverage=_d("10"),
        max_gross_exposure=_d("8.00"),
        max_concurrent_positions=6,
        max_position_notional_pct=_d("0.80"),
        min_confluence=0.34,
        signal_tf=TF.M5,
        context_tf=TF.H1,
        allow_shorts=True,
        allow_counter_trend=True,
        max_trades_per_day=40,
        cooldown_bars=0,
        atr_stop_mult=_d("1.0"),
        take_profit_r=_d("1.4"),
        breakeven_at_r=_d("0.35"),
        trail_atr_mult=_d("1.0"),
        partial_exit_pct=_d("0.7"),
        time_stop_bars=10,
        daily_loss_halt=_d("0.150"),
        max_drawdown_halt=_d("0.50"),
        loss_streak_halt=8,
        recovery_risk_factor=_d("0.25"),
        target_monthly_return=2.00,
        warnings_en=(
            "The +200%/month target is a hypothesis this level is built to TEST, not a forecast.",
            "10x liquidates on a 10% adverse move. BTC has done that in 20 minutes.",
            "Monte Carlo on this configuration puts the probability of losing half the "
            "account within 3 months in the double digits. Read the measured numbers.",
        ),
        warnings_fa=(
            "هدف ۲۰۰٪ ماهانه یک فرضیه است که این سطح برای «آزمودن» آن ساخته شده، نه یک پیش‌بینی.",
            "اهرم ۱۰ با حرکت ۱۰٪ خلاف جهت لیکویید می‌شود. بیت‌کوین این را در ۲۰ دقیقه انجام داده.",
            "شبیه‌سازی مونت‌کارلو روی این تنظیمات، احتمال از دست دادن نیمی از حساب ظرف ۳ ماه را "
            "دورقمی نشان می‌دهد. اعداد اندازه‌گیری‌شده را بخوانید.",
        ),
    ),
)

_BY_LEVEL: Final[dict[int, RiskProfile]] = {p.level: p for p in _LEVELS}


def profile(level: int) -> RiskProfile:
    """The risk profile for a level. Raises on anything outside 1..10."""
    try:
        return _BY_LEVEL[int(level)]
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"risk level must be an integer 1..10, got {level!r}") from exc


def all_profiles() -> tuple[RiskProfile, ...]:
    return _LEVELS


def ladder() -> list[dict[str, object]]:
    """The whole dial, for the UI to render in one call."""
    return [p.headline() for p in _LEVELS]


def spot_only(p: RiskProfile) -> RiskProfile:
    """Clamp a profile to leverage 1.

    Iranian venues are spot-only; running a level-9 profile there must degrade
    honestly rather than silently pretending the leverage was applied.
    """
    if not p.uses_leverage:
        return p
    return replace(
        p,
        max_leverage=Decimal("1"),
        max_gross_exposure=min(p.max_gross_exposure, Decimal("1")),
        allow_shorts=False,
        warnings_en=p.warnings_en
        + (
            f"Venue is spot-only: leverage clamped from {p.max_leverage}x to 1x and shorts "
            "disabled. Returns will be far below this level's target.",
        ),
        warnings_fa=p.warnings_fa
        + (
            f"این صرافی فقط اسپات است: اهرم از {p.max_leverage} به ۱ محدود شد و فروش "
            "استقراضی غیرفعال است. بازدهی بسیار کمتر از هدف این سطح خواهد بود.",
        ),
    )
