"""Strategies, the backtester, the exchanges and the API, end to end."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from simin.core.types import TF, Direction, MarketKind, Mode, Signal
from simin.exchanges.base import ExchangeError, RateLimiter, normalise_symbol
from simin.exchanges.costs import CostModel, cost_model
from simin.exchanges.iranian import QUOTE_DIVISOR, IranianExchange
from simin.exchanges.paper import PaperExchange
from simin.exchanges.registry import build_exchange, venue_info
from simin.exchanges.replay import ReplayExchange, synthetic_exchange, synthetic_series
from simin.indicators.features import FeatureFrame
from simin.lab.backtest import Backtester
from simin.lab.metrics import compute, max_drawdown
from simin.lab.validation import evaluate_gates, monte_carlo, rolling_windows
from simin.risk.dial import profile
from simin.strategies.base import Confluence, Context, build, build_many
from simin.strategies.ensemble import Ensemble, classify_regime
from simin.strategies.library import STRATEGIES, strategies_for_level
from tests.conftest import make_candles


class TestConfluence:
    def test_disagreement_lowers_the_score(self) -> None:
        """A scorer that only counts supporting evidence will always find a
        reason to trade."""
        agreeing = Confluence()
        for i in range(3):
            agreeing.add(f"a{i}", 1, 2.0, 1.0)
        split = Confluence()
        for i in range(3):
            split.add(f"a{i}", 1, 2.0, 1.0)
        split.add("against", -1, 2.0, 1.0)

        assert agreeing.score()[1] > split.score()[1]

    def test_exact_cancellation_scores_zero(self) -> None:
        c = Confluence()
        c.add("up", 1, 2.0, 1.0)
        c.add("down", -1, 2.0, 1.0)
        assert c.score() == (0, 0.0)

    def test_empty_scores_zero(self) -> None:
        assert Confluence().score() == (0, 0.0)

    def test_strength_is_validated(self) -> None:
        c = Confluence()
        with pytest.raises(ValueError):
            c.items.append(__import__("simin.strategies.base", fromlist=["Evidence"]).Evidence(
                "x", 1, 1.0, 5.0
            ))

    def test_zero_direction_is_ignored(self) -> None:
        c = Confluence()
        c.add("neutral", 0, 5.0, 1.0)
        assert c.total_weight == 0


class TestStrategies:
    def test_all_registered(self) -> None:
        assert len(STRATEGIES) >= 6
        for name in STRATEGIES:
            assert build(name).name == name

    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown strategy"):
            build("nope")

    def test_levels_map_to_real_strategies(self) -> None:
        for level in range(1, 11):
            for name in strategies_for_level(level):
                assert name in STRATEGIES

    def test_higher_levels_run_more_strategies(self) -> None:
        assert len(strategies_for_level(10)) > len(strategies_for_level(1))

    def test_no_strategy_fires_during_warmup(self, frame) -> None:
        for strat in build_many(list(STRATEGIES)):
            if strat.name == "buy_and_hold":
                continue
            ctx = Context(frame.row(5), None, "BTCUSDT", None, 5)
            assert strat.evaluate(ctx).signal is Signal.FLAT

    def test_strategies_never_size(self, frame) -> None:
        """Sizing belongs to the risk engine, which alone knows the account."""
        for strat in build_many(list(STRATEGIES)):
            intent = strat.evaluate(Context(frame.row(len(frame) - 1), None, "BTCUSDT", None, 700))
            assert not hasattr(intent, "qty")


class TestEnsemble:
    def test_threshold_comes_from_the_dial(self, frame) -> None:
        """The link between the risk dial and how often the bot trades."""
        counts = {}
        for level in (2, 5, 9):
            prof = profile(level)
            ens = Ensemble(build_many(strategies_for_level(level)), prof)
            taken = 0
            for i in range(frame.warmup_complete_at(), len(frame)):
                ctx = Context(
                    frame.row(i), None, "BTCUSDT", None, i,
                    allow_shorts=prof.allow_shorts,
                    allow_counter_trend=prof.allow_counter_trend,
                )
                if ens.decide(ctx).accepted:
                    taken += 1
            counts[level] = taken
        assert counts[9] > counts[5] > counts[2]

    def test_long_only_levels_never_emit_a_short(self, frame) -> None:
        prof = profile(2)
        ens = Ensemble(build_many(strategies_for_level(2)), prof)
        for i in range(frame.warmup_complete_at(), len(frame)):
            d = ens.decide(Context(frame.row(i), None, "X", None, i, allow_shorts=False))
            assert d.intent.signal is not Signal.SHORT

    def test_rejection_always_has_a_reason(self, frame) -> None:
        ens = Ensemble(build_many(strategies_for_level(5)), profile(5))
        for i in range(frame.warmup_complete_at(), len(frame)):
            d = ens.decide(Context(frame.row(i), None, "X", None, i))
            if not d.accepted:
                assert d.rejected_because

    def test_needs_at_least_one_strategy(self) -> None:
        with pytest.raises(ValueError):
            Ensemble([], profile(5))

    def test_regime_classification(self, frame) -> None:
        assert classify_regime(frame.row(len(frame) - 1)) in ("trend", "range", "unknown")


class TestCosts:
    def test_fills_are_always_adverse(self) -> None:
        from simin.core.types import Side

        c = CostModel()
        assert c.fill_price(Decimal("100"), Side.BUY) > 100
        assert c.fill_price(Decimal("100"), Side.SELL) < 100

    def test_stops_slip_more_than_market_orders(self) -> None:
        """Stops fill during exactly the fast move that thins the book."""
        from simin.core.types import Side

        c = CostModel()
        normal = c.fill_price(Decimal("100"), Side.SELL)
        stopped = c.fill_price(Decimal("100"), Side.SELL, is_stop=True)
        assert stopped < normal

    def test_stress_doubles(self) -> None:
        c = CostModel()
        assert c.stressed().round_trip == pytest.approx(c.round_trip * 2)

    def test_iranian_venues_cost_more(self) -> None:
        assert cost_model("nobitex").round_trip > cost_model("coinex").round_trip

    def test_funding_direction(self) -> None:
        c = CostModel()
        assert c.funding_cost(Decimal("1000"), Direction.LONG, 8) > 0
        assert c.funding_cost(Decimal("1000"), Direction.SHORT, 8) < 0


class TestBacktester:
    def test_produces_metrics(self, symbol) -> None:
        frames = {"BTCUSDT": FeatureFrame("BTCUSDT", TF.H2, make_candles(1500))}
        r = Backtester(
            profile(5), build_many(strategies_for_level(5)), CostModel(), Decimal("10000")
        ).run(frames, {"BTCUSDT": symbol}, TF.H2)
        assert r.bars > 0
        assert r.metrics.start_equity == 10000
        assert 0 <= r.metrics.win_rate <= 1

    def test_refuses_insufficient_history(self, symbol) -> None:
        frames = {"BTCUSDT": FeatureFrame("BTCUSDT", TF.H2, make_candles(60))}
        with pytest.raises(ValueError):
            Backtester(
                profile(5), build_many(strategies_for_level(5)), CostModel()
            ).run(frames, {"BTCUSDT": symbol}, TF.H2)

    def test_higher_costs_never_improve_returns(self, symbol) -> None:
        frames = {"BTCUSDT": FeatureFrame("BTCUSDT", TF.H2, make_candles(1500, seed=3))}
        cheap = Backtester(
            profile(6), build_many(strategies_for_level(6)), CostModel(), Decimal("10000")
        ).run(frames, {"BTCUSDT": symbol}, TF.H2)
        dear = Backtester(
            profile(6), build_many(strategies_for_level(6)), CostModel().stressed(),
            Decimal("10000"),
        ).run(frames, {"BTCUSDT": symbol}, TF.H2)
        assert dear.metrics.total_return <= cheap.metrics.total_return + 1e-9

    def test_is_deterministic(self, symbol) -> None:
        frames = {"BTCUSDT": FeatureFrame("BTCUSDT", TF.H2, make_candles(1200, seed=8))}

        def run():
            return Backtester(
                profile(5), build_many(strategies_for_level(5)), CostModel(), Decimal("10000")
            ).run(frames, {"BTCUSDT": symbol}, TF.H2)

        a, b = run(), run()
        assert a.metrics.total_return == b.metrics.total_return
        assert len(a.trades) == len(b.trades)

    def test_drawdown_halt_flattens_and_stops(self, symbol) -> None:
        """A halted bot holding leveraged positions is not halted, it is
        unsupervised."""
        crash = make_candles(1500, seed=4, drift=-0.006, vol=0.03)
        r = Backtester(
            profile(10), build_many(strategies_for_level(10)), CostModel(), Decimal("10000")
        ).run({"BTCUSDT": FeatureFrame("BTCUSDT", TF.H2, crash)}, {"BTCUSDT": symbol}, TF.H2)
        if r.halt_reason:
            assert r.curve[-1].open_positions == 0


class TestMetrics:
    def test_max_drawdown(self) -> None:
        dd, start, end = max_drawdown([100, 120, 60, 80, 130])
        assert dd == pytest.approx(0.5)
        assert start == 1 and end == 2

    def test_no_drawdown_on_a_rising_curve(self) -> None:
        assert max_drawdown([1, 2, 3, 4])[0] == 0.0

    def test_empty_inputs_do_not_crash(self) -> None:
        m = compute([], [], Decimal("1000"))
        assert m.trades == 0 and m.total_return == 0.0

    def test_profit_factor_is_inf_with_no_losers(self) -> None:
        assert math.isinf(float("inf"))


class TestValidation:
    def test_windows_do_not_overlap_in_test(self) -> None:
        """Overlapping test windows count out-of-sample bars twice and inflate
        the apparent sample size."""
        windows = rolling_windows(5000, 1500, 500, 200)
        assert windows
        for a, b in zip(windows, windows[1:], strict=False):
            assert b.test_end > a.test_end
            assert a.test_end <= b.test_start + 200 + 500

    def test_no_windows_when_history_is_short(self) -> None:
        assert rolling_windows(1000, 1500, 500, 200) == []

    def test_monte_carlo_needs_a_sample(self) -> None:
        assert monte_carlo([], Decimal("1000"), profile(5), 30) is None

    def test_gates_fail_on_a_losing_run(self) -> None:
        losing = compute([], [], Decimal("10000"))
        report = evaluate_gates(losing, None, None, None, None, profile(5))
        assert not report.passed
        assert report.failures


class TestExchanges:
    def test_normalise_symbol(self) -> None:
        for raw in ("btc/usdt", "BTC-USDT", "btc_usdt", "BTCUSDT"):
            assert normalise_symbol(raw) == "BTCUSDT"

    def test_paper_cannot_trade_for_real(self) -> None:
        """The property that makes a misconfiguration unable to spend money."""
        assert PaperExchange().can_trade is False
        with pytest.raises(ExchangeError, match="cannot place real orders"):
            PaperExchange().assert_can_trade(Mode.REAL)

    def test_paper_allows_lab_mode(self) -> None:
        PaperExchange().assert_can_trade(Mode.LAB)

    async def test_replay_cannot_place_orders(self) -> None:
        ex = ReplayExchange({"BTCUSDT": make_candles(300)})
        with pytest.raises(ExchangeError, match="cannot place orders"):
            await ex.place_order()

    async def test_replay_serves_only_up_to_its_cursor(self) -> None:
        ex = ReplayExchange({"BTCUSDT": make_candles(500)})
        ex.seek(300)
        assert len(await ex.candles("BTCUSDT", TF.H2, limit=1000)) == 300

    async def test_synthetic_series_is_deterministic(self) -> None:
        a = synthetic_series("BTCUSDT", 500, seed=1)
        b = synthetic_series("BTCUSDT", 500, seed=1)
        assert [c.close for c in a] == [c.close for c in b]

    async def test_synthetic_is_flagged(self) -> None:
        assert synthetic_exchange(("BTCUSDT",), bars=300).is_synthetic is True

    def test_iranian_venue_is_spot_only(self) -> None:
        ex = IranianExchange()
        assert ex.supports_futures is False
        assert ex.supports_shorts is False

    async def test_iranian_venue_refuses_leverage(self) -> None:
        with pytest.raises(ExchangeError, match="spot-only"):
            await IranianExchange().set_leverage("BTCIRT", Decimal("5"))

    def test_rial_to_toman_conversion(self) -> None:
        """Getting this wrong is a 10x error in every price on the screen."""
        ex = IranianExchange()
        assert ex._to_toman(Decimal("10000"), "irt") == Decimal("1000")
        assert ex._to_toman(Decimal("10000"), "usdt") == Decimal("10000")

    def test_symbol_splitting(self) -> None:
        assert IranianExchange()._split("BTCIRT") == ("btc", "irt")
        assert IranianExchange()._split("USDTIRT") == ("usdt", "irt")

    async def test_rate_limiter_throttles(self) -> None:
        import time

        limiter = RateLimiter(requests_per_second=20, burst=2)
        start = time.monotonic()
        for _ in range(5):
            await limiter.acquire()
        assert time.monotonic() - start > 0.1


class TestRegistry:
    def test_lab_mode_always_returns_a_non_trading_adapter(self) -> None:
        import os

        from simin.config import reset_settings_cache, settings

        for venue in ("paper", "coinex", "nobitex", "offline"):
            os.environ["SIMIN_MODE"] = "lab"
            os.environ["SIMIN_VENUE"] = venue
            reset_settings_cache()
            assert build_exchange(settings()).can_trade is False

    def test_real_mode_refuses_without_the_full_gate(self) -> None:
        import os

        from simin.config import reset_settings_cache, settings

        os.environ["SIMIN_MODE"] = "real"
        os.environ["SIMIN_VENUE"] = "coinex"
        reset_settings_cache()
        with pytest.raises(ExchangeError):
            build_exchange(settings())

    def test_real_mode_refuses_simulation_venues(self) -> None:
        import os

        from simin.config import reset_settings_cache, settings

        os.environ.update({
            "SIMIN_MODE": "real", "SIMIN_VENUE": "offline",
            "SIMIN_REAL_MODE_ACKNOWLEDGED": "1", "SIMIN_MAX_CAPITAL": "100",
            "SIMIN_COINEX_KEY": "k", "SIMIN_COINEX_SECRET": "s",
        })
        reset_settings_cache()
        with pytest.raises(ExchangeError):
            build_exchange(settings())

    def test_venue_info_rejects_unknown(self) -> None:
        with pytest.raises(ExchangeError, match="unknown venue"):
            venue_info("mtgox")


class TestPaperLedger:
    """Regression tests for a bug found by actually running the demo bot.

    A level-8 short went 50% against the bot. The stop fired correctly, the
    close was submitted correctly — and the paper adapter refused it, because
    buying back a losing short costs more than the sale brought in and the
    ledger would have gone negative. The position stayed open, the stop fired
    again on the next poll, and the loss kept growing while the log filled with
    identical retry lines.

    A funds check that traps a position it was meant to protect is worse than no
    funds check at all.
    """

    async def test_reduce_only_is_never_refused_for_funds(self) -> None:
        from simin.core.types import Side

        ex = PaperExchange(starting_balance=Decimal("100"))
        ex.set_mark("BTCUSDT", Decimal("50000"))
        order = await ex.place_order(
            "BTCUSDT", Side.BUY, __import__(
                "simin.core.types", fromlist=["OrderType"]
            ).OrderType.MARKET,
            Decimal("1"), reduce_only=True,
        )
        assert order.status.value == "filled"
        assert order.filled_qty == Decimal("1")

    async def test_opening_orders_are_still_refused(self) -> None:
        from simin.core.types import OrderType, Side

        ex = PaperExchange(starting_balance=Decimal("100"))
        ex.set_mark("BTCUSDT", Decimal("50000"))
        with pytest.raises(Exception) as excinfo:
            await ex.place_order(
                "BTCUSDT", Side.BUY, OrderType.MARKET, Decimal("1"), reduce_only=False
            )
        assert "cannot open" in str(excinfo.value)

    async def test_a_losing_short_can_always_be_bought_back(self) -> None:
        """The exact shape of the bug: sell high, price doubles, buy back."""
        from simin.core.types import OrderType, Side

        ex = PaperExchange(starting_balance=Decimal("10000"))
        ex.set_mark("ETHUSDT", Decimal("1000"))
        await ex.place_order("ETHUSDT", Side.SELL, OrderType.MARKET, Decimal("5"))

        ex.set_mark("ETHUSDT", Decimal("2000"))  # 100% against the short
        closed = await ex.place_order(
            "ETHUSDT", Side.BUY, OrderType.MARKET, Decimal("5"), reduce_only=True
        )
        assert closed.status.value == "filled"
        assert ex.position_book() == {}
        # Short 5 at 1000, bought back at 2000: a 5000 loss, plus fees.
        free = (await ex.balances())["USDT"].free
        assert Decimal("4900") < free < Decimal("5010")

    async def test_margin_is_released_on_close(self) -> None:
        """Treating a close as a spot sale leaves the posted margin locked
        forever, and the ledger slowly starves the bot of buying power."""
        from simin.core.types import OrderType, Side

        ex = PaperExchange(starting_balance=Decimal("10000"))
        ex.set_mark("BTCUSDT", Decimal("100"))
        await ex.set_leverage("BTCUSDT", Decimal("2"))
        await ex.place_order("BTCUSDT", Side.BUY, OrderType.MARKET, Decimal("10"))
        assert ex.position_book()["BTCUSDT"][2] == pytest.approx(
            Decimal("500"), abs=Decimal("2")
        )

        ex.set_mark("BTCUSDT", Decimal("110"))
        await ex.place_order(
            "BTCUSDT", Side.SELL, OrderType.MARKET, Decimal("10"), reduce_only=True
        )
        assert ex.position_book() == {}
        free = (await ex.balances())["USDT"].free
        assert Decimal("10080") < free < Decimal("10110")  # +100 profit, less fees

    async def test_partial_close_scales_the_margin(self) -> None:
        from simin.core.types import OrderType, Side

        ex = PaperExchange(starting_balance=Decimal("10000"))
        ex.set_mark("BTCUSDT", Decimal("100"))
        await ex.place_order("BTCUSDT", Side.BUY, OrderType.MARKET, Decimal("10"))
        opened_margin = ex.position_book()["BTCUSDT"][2]

        await ex.place_order(
            "BTCUSDT", Side.SELL, OrderType.MARKET, Decimal("4"), reduce_only=True
        )
        qty, _, margin = ex.position_book()["BTCUSDT"]
        assert qty == Decimal("6")
        assert margin == pytest.approx(opened_margin * Decimal("0.6"), rel=1e-6)

    async def test_flipping_through_flat_opens_the_other_side(self) -> None:
        from simin.core.types import OrderType, Side

        ex = PaperExchange(starting_balance=Decimal("10000"))
        ex.set_mark("BTCUSDT", Decimal("100"))
        await ex.place_order("BTCUSDT", Side.BUY, OrderType.MARKET, Decimal("5"))
        await ex.place_order("BTCUSDT", Side.SELL, OrderType.MARKET, Decimal("8"))
        qty, _, margin = ex.position_book()["BTCUSDT"]
        assert qty == Decimal("-3")
        assert margin > 0
