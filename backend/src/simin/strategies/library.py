"""The strategies themselves.

Each one is built for a specific market condition and says so via `regime`. The
ensemble mutes strategies running outside their condition, because the single
fastest way to lose money with indicators is to run a mean-reversion system in
a trend — every oversold reading is a buy, and the market keeps going down.

Weights inside each strategy are the tuning surface. They are deliberately
plain numbers rather than optimised parameters: a weight fitted to history is a
weight fitted to noise, and the walk-forward in `simin.lab` exists to catch
exactly that. What gets optimised is the risk dial, not the beliefs.
"""

from __future__ import annotations

from simin.core.types import Direction, Intent, Signal
from simin.priceaction.patterns import PatternKind
from simin.priceaction.structure import Structure, StructureEvent, nearest_level
from simin.strategies.base import Confluence, Context, Strategy

# Regime thresholds. ADX is the arbiter of trending vs ranging.
ADX_TRENDING = 25.0
ADX_CHOPPY = 20.0


class OscillationReversion(Strategy):
    """Fade stretched moves back to the mean — the core 'oscillation' engine.

    Only fires in a range (low ADX). In a trend this same logic is a wealth
    transfer to whoever is on the other side, which is why the regime gate is
    not optional here.

    The setup: price is at a Bollinger extreme, RSI confirms the stretch, the
    z-score says it is statistically far, and ideally a rejection candle prints
    at a level that has held before.
    """

    name = "oscillation"
    name_fa = "نوسان‌گیری"
    regime = "range"
    warmup = 210
    description = "Fades stretched price back to the mean inside a range."
    description_fa = "بازگشت قیمت کشیده‌شده به میانگین در محدوده رِنج."

    def evaluate(self, ctx: Context) -> Intent:
        r = ctx.row
        v = r.require("rsi", "bb_pct", "zscore", "adx", "atr", "bb_width")
        if v is None:
            return self.flat()
        rsi, bb_pct, z, adx, atr, bb_width = v

        # Hard regime gate: a strong trend disqualifies the whole idea.
        if adx > ADX_TRENDING:
            return self.flat()

        conf = Confluence()
        # How far into the band's tail price sits. Below 0 / above 1 means the
        # close is outside the band entirely.
        if bb_pct <= 0.10:
            conf.add("bollinger_lower", 1, 3.0, min((0.10 - bb_pct) / 0.10 + 0.4, 1.0),
                     f"%B={bb_pct:.2f}")
        elif bb_pct >= 0.90:
            conf.add("bollinger_upper", -1, 3.0, min((bb_pct - 0.90) / 0.10 + 0.4, 1.0),
                     f"%B={bb_pct:.2f}")

        if rsi <= 32:
            conf.add("rsi_oversold", 1, 2.5, min((32 - rsi) / 17, 1.0), f"RSI={rsi:.0f}")
        elif rsi >= 68:
            conf.add("rsi_overbought", -1, 2.5, min((rsi - 68) / 17, 1.0), f"RSI={rsi:.0f}")

        if abs(z) >= 1.5:
            conf.add("zscore_stretch", -1 if z > 0 else 1, 2.0, min((abs(z) - 1.5) / 1.5, 1.0),
                     f"z={z:+.1f}")

        # Range confirmation: the flatter ADX is, the more we trust the fade.
        conf.add("range_regime", 1 if bb_pct < 0.5 else -1, 1.5,
                 min((ADX_CHOPPY - adx) / ADX_CHOPPY, 1.0) if adx < ADX_CHOPPY else 0.2,
                 f"ADX={adx:.0f}")

        # A rejection candle is what turns "cheap" into "turning".
        for p in r.patterns:
            if p.kind in (PatternKind.PIN_BAR_BULL, PatternKind.PIN_BAR_BEAR,
                          PatternKind.ENGULFING_BULL, PatternKind.ENGULFING_BEAR,
                          PatternKind.MORNING_STAR, PatternKind.EVENING_STAR):
                conf.add(f"pa_{p.kind.value}", p.bias, 2.0, p.strength)

        # Reversion into a level that has already held is the highest-quality
        # version of this trade.
        direction_guess = 1 if bb_pct < 0.5 else -1
        level = nearest_level(r.levels, ctx.price, above=direction_guess < 0)
        if level is not None and atr > 0:
            distance = abs(level.price - ctx.price) / atr
            if distance < 1.0:
                conf.add("at_level", direction_guess, 2.0, level.strength * (1 - distance),
                         f"{level.touches} touches")

        # A squeeze means the band is narrow and a breakout is more likely than
        # a fade. Evidence against the whole premise.
        if bb_width is not None and bb_width < 0.02:
            conf.add("squeeze_risk", -direction_guess, 1.5, 0.8, "bands compressed")

        # Counter-trend guard: fading against the higher timeframe is the
        # version of this trade that loses.
        hb = ctx.higher_bias
        if hb != 0 and direction_guess == -hb and not ctx.allow_counter_trend:
            return self.flat()
        if hb != 0:
            conf.add("htf_alignment", hb, 1.5, 0.7)

        direction, confidence = conf.score()
        stop_mult = 1.5 if abs(z) > 2.0 else 2.0
        return self._intent(ctx, direction, confidence, conf, stop_mult)


class MacdMomentum(Strategy):
    """MACD crossover in the direction of the trend, with histogram expansion.

    The histogram slope is what separates a real cross from the dozens of
    cosmetic ones that happen when MACD hovers around its signal in chop: a
    cross whose histogram is not growing has no momentum behind it.
    """

    name = "macd_momentum"
    name_fa = "مومنتوم مکدی"
    regime = "trend"
    warmup = 210
    description = "MACD crossovers filtered by trend and histogram expansion."
    description_fa = "تقاطع مکدی با فیلتر روند و گسترش هیستوگرام."

    def __init__(self) -> None:
        self._prev_hist: dict[str, float] = {}

    def evaluate(self, ctx: Context) -> Intent:
        r = ctx.row
        v = r.require("macd", "macd_signal", "macd_hist", "ema_trend", "adx", "rsi", "atr")
        if v is None:
            return self.flat()
        macd, signal, hist, ema_trend, adx, rsi, atr = v

        prev = self._prev_hist.get(ctx.symbol)
        self._prev_hist[ctx.symbol] = hist
        if prev is None:
            return self.flat()

        crossed_up = prev <= 0 < hist
        crossed_down = prev >= 0 > hist
        if not (crossed_up or crossed_down):
            # Not a cross, but an already-established and still-expanding
            # histogram is a valid continuation entry.
            if abs(hist) <= abs(prev):
                return self.flat()

        d = 1 if hist > 0 else -1
        conf = Confluence()
        conf.add("macd_cross" if (crossed_up or crossed_down) else "macd_expanding", d, 3.0,
                 1.0 if (crossed_up or crossed_down) else 0.6,
                 f"hist={hist:+.4f}")

        # Momentum must be growing, not merely positive.
        growth = (abs(hist) - abs(prev)) / max(abs(prev), 1e-9)
        conf.add("hist_expansion", d if growth > 0 else -d, 2.0, min(abs(growth), 1.0))

        # Trend filter — the single most valuable line in this strategy.
        above_trend = ctx.price > ema_trend
        conf.add("trend_filter", 1 if above_trend else -1, 3.0, 1.0,
                 "above 200EMA" if above_trend else "below 200EMA")

        conf.add("adx_strength", d, 2.0,
                 min(max((adx - ADX_CHOPPY) / 25.0, 0.0), 1.0), f"ADX={adx:.0f}")

        # Do not buy something already overbought — that is chasing.
        if d > 0 and rsi > 72:
            conf.add("rsi_extended", -1, 2.0, min((rsi - 72) / 18, 1.0), f"RSI={rsi:.0f}")
        elif d < 0 and rsi < 28:
            conf.add("rsi_extended", 1, 2.0, min((28 - rsi) / 18, 1.0), f"RSI={rsi:.0f}")

        st = r.get("supertrend_dir")
        if st is not None:
            conf.add("supertrend", int(st), 1.5, 0.8)

        vol = r.get("vol_ratio")
        if vol is not None and vol > 1.2:
            conf.add("volume_support", d, 1.0, min((vol - 1.2) / 1.3, 1.0), f"{vol:.1f}x avg")

        hb = ctx.higher_bias
        if hb != 0:
            conf.add("htf_alignment", hb, 2.0, 0.9)

        direction, confidence = conf.score()
        return self._intent(ctx, direction, confidence, conf, 2.0)


class TrendPullback(Strategy):
    """Buy the dip inside an established uptrend (and the rally in a downtrend).

    The highest-expectancy retail setup there is, and the one most often ruined
    by taking it in a downtrend. Structure has to agree before the pullback
    counts as a pullback rather than the start of a reversal.
    """

    name = "trend_pullback"
    name_fa = "اصلاح روند"
    regime = "trend"
    warmup = 210
    description = "Enters pullbacks to the moving average within a confirmed trend."
    description_fa = "ورود در اصلاح‌های قیمت به میانگین متحرک در دل یک روند تأییدشده."

    def evaluate(self, ctx: Context) -> Intent:
        r = ctx.row
        v = r.require("ema_fast", "ema_slow", "ema_trend", "rsi", "atr", "adx")
        if v is None:
            return self.flat()
        ema_fast, ema_slow, ema_trend, rsi, atr, adx = v
        if atr <= 0 or adx < ADX_CHOPPY:
            return self.flat()

        up = ema_fast > ema_slow > ema_trend
        down = ema_fast < ema_slow < ema_trend
        if not (up or down):
            return self.flat()
        d = 1 if up else -1

        # Depth of the pullback, in ATR units, measured from the fast EMA.
        depth = (ema_fast - ctx.price) / atr * d
        if depth < 0.2:
            return self.flat()  # no pullback yet, this is a breakout not a dip
        if depth > 3.5:
            return self.flat()  # too deep — the trend is probably breaking

        conf = Confluence()
        conf.add("ema_stack", d, 3.0, 1.0, "20>50>200" if up else "20<50<200")
        conf.add("pullback_depth", d, 2.5, min(depth / 2.0, 1.0), f"{depth:.1f} ATR to EMA20")

        # RSI should have cooled but not broken. A trend pullback that drives
        # RSI to 20 is not a pullback.
        if d > 0:
            conf.add("rsi_reset", 1 if 38 <= rsi <= 58 else -1, 2.0,
                     0.9 if 38 <= rsi <= 58 else min(abs(rsi - 48) / 30, 1.0), f"RSI={rsi:.0f}")
        else:
            conf.add("rsi_reset", -1 if 42 <= rsi <= 62 else 1,
                     2.0, 0.9 if 42 <= rsi <= 62 else min(abs(rsi - 52) / 30, 1.0),
                     f"RSI={rsi:.0f}")

        conf.add("adx_trending", d, 2.0, min(max((adx - ADX_CHOPPY) / 25, 0.0), 1.0),
                 f"ADX={adx:.0f}")

        # Structure must not have flipped against us.
        s = r.structure
        if d > 0 and s.structure is Structure.DOWNTREND:
            conf.add("structure_broken", -1, 3.0, 1.0, "lower lows")
        elif d < 0 and s.structure is Structure.UPTREND:
            conf.add("structure_broken", 1, 3.0, 1.0, "higher highs")
        else:
            conf.add("structure_intact", d, 2.0, 0.8)

        if s.event in (StructureEvent.CHOCH_UP, StructureEvent.CHOCH_DOWN):
            flip = 1 if s.event is StructureEvent.CHOCH_UP else -1
            conf.add("choch_warning", flip, 2.0, 0.9, "character change")

        # A resumption candle is the trigger.
        for p in r.patterns:
            if p.bias == d and p.kind in (
                PatternKind.PIN_BAR_BULL, PatternKind.PIN_BAR_BEAR,
                PatternKind.ENGULFING_BULL, PatternKind.ENGULFING_BEAR,
                PatternKind.MARUBOZU_BULL, PatternKind.MARUBOZU_BEAR,
            ):
                conf.add(f"pa_{p.kind.value}", p.bias, 2.0, p.strength)

        hb = ctx.higher_bias
        if hb != 0:
            conf.add("htf_alignment", hb, 2.0, 0.9)

        direction, confidence = conf.score()
        return self._intent(ctx, direction, confidence, conf, 2.2)


class StructureBreakout(Strategy):
    """Trade the break of structure out of a volatility squeeze.

    Volatility is mean-reverting even when price is not: compressed ranges
    resolve into expansion. The squeeze tells us *when*; the BOS tells us
    *which way*; volume tells us whether anyone actually showed up.
    """

    name = "breakout"
    name_fa = "شکست ساختار"
    regime = "any"
    warmup = 210
    description = "Breaks of market structure out of a volatility squeeze."
    description_fa = "شکست ساختار بازار پس از فشردگی نوسان."

    def evaluate(self, ctx: Context) -> Intent:
        r = ctx.row
        v = r.require("bb_width", "atr", "adx", "vol_ratio", "kc_upper", "kc_lower",
                      "bb_upper", "bb_lower")
        if v is None:
            return self.flat()
        bb_width, atr, adx, vol_ratio, kc_up, kc_dn, bb_up, bb_dn = v

        s = r.structure
        if s.event not in (StructureEvent.BOS_UP, StructureEvent.BOS_DOWN,
                           StructureEvent.CHOCH_UP, StructureEvent.CHOCH_DOWN):
            return self.flat()
        d = 1 if s.event in (StructureEvent.BOS_UP, StructureEvent.CHOCH_UP) else -1

        conf = Confluence()
        is_bos = s.event in (StructureEvent.BOS_UP, StructureEvent.BOS_DOWN)
        conf.add("bos" if is_bos else "choch", d, 3.5, 1.0 if is_bos else 0.7,
                 s.event.value)

        # TTM-style squeeze: Bollinger bands inside Keltner channels means
        # volatility is unusually compressed and about to expand.
        squeezed = bb_up < kc_up and bb_dn > kc_dn
        conf.add("squeeze_release", d, 2.5, 0.95 if squeezed else 0.25,
                 "BB inside KC" if squeezed else f"width={bb_width:.3f}")

        # A breakout on below-average volume is usually a fake.
        if vol_ratio >= 1.3:
            conf.add("volume_confirm", d, 2.5, min((vol_ratio - 1.3) / 1.7, 1.0),
                     f"{vol_ratio:.1f}x avg")
        else:
            conf.add("volume_missing", -d, 2.0, min((1.3 - vol_ratio) / 0.8, 1.0),
                     f"only {vol_ratio:.1f}x avg")

        # The close must be decisive, not a wick poking through.
        body = r.get("candle_body_pct")
        if body is not None:
            conf.add("decisive_close" if body > 0.55 else "weak_close",
                     d if body > 0.55 else -d, 1.5,
                     abs(body - 0.55) / 0.45)

        conf.add("adx_confirm", d, 1.5, min(max((adx - ADX_CHOPPY) / 25, 0.0), 1.0),
                 f"ADX={adx:.0f}")

        obv_slope = r.get("obv_slope")
        if obv_slope is not None and abs(obv_slope) > 1e-9:
            conf.add("flow", 1 if obv_slope > 0 else -1, 1.5, min(abs(obv_slope) * 20, 1.0))

        hb = ctx.higher_bias
        if hb != 0:
            conf.add("htf_alignment", hb, 2.0, 0.85)

        direction, confidence = conf.score()
        # Breakouts need room: a tight stop inside the range being broken is
        # exactly where the retest goes.
        return self._intent(ctx, direction, confidence, conf, 2.5)


class RsiDivergence(Strategy):
    """Price makes a new extreme; RSI does not. The move is running on fumes.

    Divergence is notorious for firing early and repeatedly in strong trends, so
    this requires a structural CHoCH or a rejection candle as the trigger — the
    divergence sets up the trade, it does not enter it.
    """

    name = "rsi_divergence"
    name_fa = "واگرایی RSI"
    regime = "any"
    warmup = 240
    description = "Momentum divergence confirmed by a change of character."
    description_fa = "واگرایی مومنتوم با تأیید تغییر کاراکتر بازار."
    lookback = 40

    def __init__(self) -> None:
        self._history: dict[str, list[tuple[float, float, float]]] = {}

    def evaluate(self, ctx: Context) -> Intent:
        r = ctx.row
        v = r.require("rsi", "atr", "adx")
        if v is None:
            return self.flat()
        rsi, atr, adx = v

        hist = self._history.setdefault(ctx.symbol, [])
        hist.append((float(r.candle.high), float(r.candle.low), rsi))
        if len(hist) > self.lookback:
            del hist[0]
        if len(hist) < self.lookback:
            return self.flat()

        highs = [h for h, _, _ in hist]
        lows = [lo for _, lo, _ in hist]
        rsis = [x for _, _, x in hist]
        recent, prior = slice(-10, None), slice(0, -15)

        conf = Confluence()
        d = 0
        # Bearish: higher price high, lower RSI high.
        if max(highs[recent]) > max(highs[prior]) and max(rsis[recent]) < max(rsis[prior]) - 3:
            gap = max(rsis[prior]) - max(rsis[recent])
            conf.add("bearish_divergence", -1, 3.5, min(gap / 15, 1.0), f"RSI −{gap:.0f}")
            d = -1
        # Bullish: lower price low, higher RSI low.
        elif min(lows[recent]) < min(lows[prior]) and min(rsis[recent]) > min(rsis[prior]) + 3:
            gap = min(rsis[recent]) - min(rsis[prior])
            conf.add("bullish_divergence", 1, 3.5, min(gap / 15, 1.0), f"RSI +{gap:.0f}")
            d = 1
        if d == 0:
            return self.flat()

        # Trigger requirement — divergence alone is not an entry.
        s = r.structure
        triggered = (
            (d > 0 and s.event is StructureEvent.CHOCH_UP)
            or (d < 0 and s.event is StructureEvent.CHOCH_DOWN)
            or any(p.bias == d and p.strength > 0.4 for p in r.patterns)
        )
        if not triggered:
            return self.flat()
        conf.add("trigger", d, 2.5, 1.0, s.event.value if s.event.value != "none" else "candle")

        # A very strong trend eats divergences alive.
        conf.add("trend_hazard", -d, 2.0, min(max((adx - 35) / 25, 0.0), 1.0), f"ADX={adx:.0f}")

        if d > 0 and rsi < 40:
            conf.add("rsi_position", 1, 1.5, min((40 - rsi) / 20, 1.0))
        elif d < 0 and rsi > 60:
            conf.add("rsi_position", -1, 1.5, min((rsi - 60) / 20, 1.0))

        hb = ctx.higher_bias
        if hb != 0:
            conf.add("htf_alignment", hb, 1.5, 0.7)

        direction, confidence = conf.score()
        return self._intent(ctx, direction, confidence, conf, 1.8)


class StochScalp(Strategy):
    """Fast stochastic reversals for the low-timeframe, high-risk dial settings.

    Deliberately simple and deliberately quick. At 5m and 15m the fee drag is
    the dominant term, so this refuses any setup where the expected move is
    small relative to volatility — a scalp that cannot clear costs is a
    donation.
    """

    name = "stoch_scalp"
    name_fa = "اسکالپ استوکاستیک"
    regime = "range"
    warmup = 210
    description = "Fast stochastic reversals on low timeframes, cost-aware."
    description_fa = "بازگشت‌های سریع استوکاستیک در تایم‌فریم پایین، با لحاظ کارمزد."
    #: Round-trip cost assumption. A setup whose 1-ATR move cannot clear this
    #: multiple of it is rejected outright.
    min_atr_over_cost = 4.0
    round_trip_cost = 0.0012

    def __init__(self) -> None:
        self._prev: dict[str, tuple[float, float]] = {}

    def evaluate(self, ctx: Context) -> Intent:
        r = ctx.row
        v = r.require("stoch_k", "stoch_d", "rsi_fast", "atr", "atr_pct", "adx", "ema_fast")
        if v is None:
            return self.flat()
        k, d_line, rsi_f, atr, atr_pct, adx, ema_fast = v

        prev = self._prev.get(ctx.symbol)
        self._prev[ctx.symbol] = (k, d_line)
        if prev is None:
            return self.flat()
        pk, pd = prev

        # Cost gate, applied before anything else. This is the line that keeps
        # levels 7-10 from grinding the account down through pure friction.
        if atr_pct < self.min_atr_over_cost * self.round_trip_cost:
            return self.flat()

        cross_up = pk <= pd and k > d_line and k < 35
        cross_down = pk >= pd and k < d_line and k > 65
        if not (cross_up or cross_down):
            return self.flat()
        dr = 1 if cross_up else -1

        conf = Confluence()
        conf.add("stoch_cross", dr, 3.0, 1.0, f"%K={k:.0f}")
        extreme = (35 - k) / 35 if dr > 0 else (k - 65) / 35
        conf.add("stoch_extreme", dr, 2.0, max(min(extreme, 1.0), 0.0))

        if dr > 0 and rsi_f < 40:
            conf.add("rsi_fast_confirm", 1, 2.0, min((40 - rsi_f) / 25, 1.0))
        elif dr < 0 and rsi_f > 60:
            conf.add("rsi_fast_confirm", -1, 2.0, min((rsi_f - 60) / 25, 1.0))

        # Scalping into a strong trend against it is the fastest way to lose.
        if adx > ADX_TRENDING:
            trend_dir = 1 if ctx.price > ema_fast else -1
            conf.add("trend_pressure", trend_dir, 2.5, min((adx - ADX_TRENDING) / 25, 1.0),
                     f"ADX={adx:.0f}")

        conf.add("volatility_ok", dr, 1.5,
                 min(atr_pct / (self.min_atr_over_cost * self.round_trip_cost) - 1, 1.0),
                 f"ATR {atr_pct * 100:.2f}%")

        for p in r.patterns:
            if p.bias == dr:
                conf.add(f"pa_{p.kind.value}", p.bias, 1.5, p.strength)

        direction, confidence = conf.score()
        return self._intent(ctx, direction, confidence, conf, 1.5)


class BuyAndHold(Strategy):
    """Not a strategy — the benchmark every other one has to beat.

    Without this in the comparison, "the bot made 8%" is unanswerable. If the
    asset made 30% over the same window, the bot destroyed value.
    """

    name = "buy_and_hold"
    name_fa = "خرید و نگهداری"
    regime = "any"
    warmup = 2
    description = "Benchmark: buy at the start, hold to the end."
    description_fa = "معیار سنجش: خرید در ابتدا و نگه‌داشتن تا انتها."

    def evaluate(self, ctx: Context) -> Intent:
        if ctx.in_position or ctx.bar_index < self.warmup:
            return self.flat()
        return Intent(Signal.LONG, 1.0, stop_price=None, strategy=self.name,
                      reasons=("benchmark",))


STRATEGIES: dict[str, type[Strategy]] = {
    OscillationReversion.name: OscillationReversion,
    MacdMomentum.name: MacdMomentum,
    TrendPullback.name: TrendPullback,
    StructureBreakout.name: StructureBreakout,
    RsiDivergence.name: RsiDivergence,
    StochScalp.name: StochScalp,
    BuyAndHold.name: BuyAndHold,
}

#: Which strategies each dial level runs. Low levels want few, selective,
#: slow strategies; high levels want everything firing on fast candles.
def strategies_for_level(level: int) -> tuple[str, ...]:
    if level <= 2:
        return (TrendPullback.name, MacdMomentum.name)
    if level <= 4:
        return (TrendPullback.name, MacdMomentum.name, OscillationReversion.name,
                StructureBreakout.name)
    if level <= 6:
        return (TrendPullback.name, MacdMomentum.name, OscillationReversion.name,
                StructureBreakout.name, RsiDivergence.name)
    return (TrendPullback.name, MacdMomentum.name, OscillationReversion.name,
            StructureBreakout.name, RsiDivergence.name, StochScalp.name)
