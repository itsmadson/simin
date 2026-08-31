"""Universe selection, multiple-testing correction, and portfolio structure.

The statistical tests here are the ones that matter most in this file. A
screener that cannot reject noise is worse than no screener, because it
launders luck into apparent evidence and hands it back with a confidence score
attached.
"""

from __future__ import annotations

import math
import random
import statistics
from decimal import Decimal

import pytest

from simin.core.types import TF, MarketKind, Symbol
from simin.exchanges.base import DepthLevel, OrderBook
from simin.indicators.features import FeatureFrame
from simin.lab.portfolio import (
    analyse,
    cluster_symbols,
    correlation,
    effective_breadth,
)
from simin.lab.screen import (
    _moments,
    deflated_sharpe,
    expected_max_sharpe,
    per_trade_sharpe,
)
from simin.lab.universe import ScanLimits, Verdict, scan
from tests.conftest import make_candles


def book(levels: list[tuple[str, str]], side: str = "asks") -> OrderBook:
    from datetime import UTC, datetime

    depth = tuple(DepthLevel(Decimal(p), Decimal(q)) for p, q in levels)
    other = (DepthLevel(Decimal("1"), Decimal("1")),)
    return OrderBook(
        symbol="X",
        bids=depth if side == "bids" else other,
        asks=depth if side == "asks" else other,
        ts=datetime.now(UTC),
    )


class TestOrderBook:
    def test_sweep_measures_walking_the_book(self) -> None:
        # 10 @ 100, then 10 @ 101. $1500 takes all of the first level and ~5 of
        # the second: average 100.33, i.e. 0.33% above best.
        ob = book([("100", "10"), ("101", "10")])
        slip = ob.sweep(Decimal("1500"))
        assert slip is not None
        assert float(slip) == pytest.approx(0.0033, abs=0.0005)

    def test_a_deep_book_costs_nothing(self) -> None:
        ob = book([("100", "10000")])
        assert float(ob.sweep(Decimal("5000"))) == pytest.approx(0.0, abs=1e-9)

    def test_a_book_that_cannot_fill_returns_none(self) -> None:
        """The answer that matters most, and the one a spread estimate can
        never give: there is not enough size here at any price."""
        ob = book([("100", "1")])
        assert ob.sweep(Decimal("50000")) is None

    def test_slippage_rises_with_size(self) -> None:
        ob = book([("100", "5"), ("101", "5"), ("105", "5"), ("120", "50")])
        sizes = [Decimal("400"), Decimal("900"), Decimal("1400"), Decimal("4000")]
        slips = [ob.sweep(s) for s in sizes]
        assert all(s is not None for s in slips)
        assert [float(s) for s in slips] == sorted(float(s) for s in slips)


class TestUniverseScan:
    class FakeExchange:
        name = "fake"

        def __init__(self, markets, tickers, books):
            self._markets, self._tickers, self._books = markets, tickers, books

        async def symbols(self):
            return self._markets

        async def tickers(self):
            return self._tickers

        async def order_book(self, symbol, limit=50):
            return self._books.get(symbol)

        async def candles(self, symbol, tf, limit=500, end=None):
            return make_candles(limit)

    def _sym(self, name: str) -> Symbol:
        return Symbol(name[:-4], "USDT", "fake", name, MarketKind.FUTURES, 2, 6,
                      max_leverage=10)

    async def test_thin_markets_are_excluded_with_a_reason(self) -> None:
        from simin.exchanges.costs import CostModel

        markets = [self._sym("BIGUSDT"), self._sym("THINUSDT")]
        tickers = [
            {"market": "BIGUSDT", "last": "100", "high": "104", "low": "97",
             "value": "50000000"},
            {"market": "THINUSDT", "last": "100", "high": "104", "low": "97",
             "value": "20000"},
        ]
        books = {"BIGUSDT": book([("100", "100000")]),
                 "THINUSDT": book([("100", "100000")])}
        ex = self.FakeExchange(markets, tickers, books)

        report = await scan(ex, CostModel(), Decimal("10000"), Decimal("3000"),
                            TF.H2, check_history=False)
        by = {m.symbol: m for m in report.markets}
        assert by["BIGUSDT"].verdict is Verdict.TRADEABLE
        assert by["THINUSDT"].verdict is Verdict.THIN
        assert "turnover" in by["THINUSDT"].reason

    async def test_position_too_large_for_the_market_is_rejected(self) -> None:
        """The same market is tradeable at $500 and not at $50,000. Calling a
        market illiquid without naming a size says nothing."""
        from simin.exchanges.costs import CostModel

        markets = [self._sym("MIDUSDT")]
        tickers = [{"market": "MIDUSDT", "last": "100", "high": "106", "low": "96",
                    "value": "1000000"}]
        books = {"MIDUSDT": book([("100", "1000000")])}
        ex = self.FakeExchange(markets, tickers, books)

        small = await scan(ex, CostModel(), Decimal("10000"), Decimal("500"),
                           TF.H2, check_history=False)
        assert small.markets[0].verdict is Verdict.TRADEABLE

        large = await scan(ex, CostModel(), Decimal("500000"), Decimal("50000"),
                           TF.H2, check_history=False)
        assert large.markets[0].verdict is Verdict.THIN
        assert "turnover" in large.markets[0].reason

    async def test_a_market_that_does_not_move_is_rejected(self) -> None:
        from simin.exchanges.costs import CostModel

        markets = [self._sym("FLATUSDT")]
        tickers = [{"market": "FLATUSDT", "last": "100", "high": "100.2",
                    "low": "99.9", "value": "9000000"}]
        ex = self.FakeExchange(markets, tickers, {"FLATUSDT": book([("100", "99999")])})
        report = await scan(ex, CostModel(), Decimal("10000"), Decimal("3000"),
                            TF.H2, check_history=False)
        assert report.markets[0].verdict is Verdict.TOO_QUIET

    async def test_stablecoins_are_not_directional_markets(self) -> None:
        from simin.exchanges.costs import CostModel

        markets = [Symbol("USDC", "USDT", "fake", "USDCUSDT", MarketKind.FUTURES, 2, 6)]
        tickers = [{"market": "USDCUSDT", "last": "1", "high": "1.001",
                    "low": "0.999", "value": "90000000"}]
        ex = self.FakeExchange(markets, tickers, {})
        report = await scan(ex, CostModel(), Decimal("10000"), Decimal("3000"),
                            TF.H2, check_history=False)
        assert report.markets[0].verdict is Verdict.STABLE


class TestDeflatedSharpe:
    """The correction that stops a search from manufacturing evidence."""

    def test_the_bar_rises_with_the_number_of_trials(self) -> None:
        var = 1 / 199
        bars = [expected_max_sharpe(t, var) for t in (1, 10, 50, 230, 2300)]
        assert bars == sorted(bars)
        assert bars[0] == 0.0

    def test_the_same_result_is_less_credible_after_a_wider_search(self) -> None:
        scores = [deflated_sharpe(0.13, 200, t) for t in (1, 10, 50, 230)]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] > 0.95      # one honest test
        assert scores[-1] < 0.5      # the same number, found by searching 230

    def test_rejects_the_best_of_many_zero_edge_strategies(self) -> None:
        """The central test. 230 coin flips; the luckiest looks superb."""
        rng = random.Random(1)
        best = max(
            per_trade_sharpe([rng.gauss(0, 1) for _ in range(200)])
            for _ in range(230)
        )
        # Annualised, this reads like a world-class strategy.
        assert best * math.sqrt(200) > 2.0
        assert deflated_sharpe(best, 200, 230) < 0.95

    def test_accepts_a_genuinely_large_edge(self) -> None:
        rng = random.Random(2)
        sr = per_trade_sharpe([rng.gauss(0.5, 1) for _ in range(200)])
        assert deflated_sharpe(sr, 200, 230) > 0.95

    def test_false_positive_rate_is_low(self) -> None:
        false_positives = 0
        for seed in range(120):
            rng = random.Random(7000 + seed)
            best = max(
                per_trade_sharpe([rng.gauss(0, 1) for _ in range(200)])
                for _ in range(23)
            )
            if deflated_sharpe(best, 200, 23) > 0.95:
                false_positives += 1
        assert false_positives / 120 < 0.10

    def test_non_normality_penalises_an_above_bar_result(self) -> None:
        """Negative skew and fat tails inflate a naive Sharpe. Above the
        threshold — the regime where an accept decision is made — they must
        reduce confidence."""
        sr = 0.30
        base = deflated_sharpe(sr, 200, 50)
        assert deflated_sharpe(sr, 200, 50, skew=-1.5) < base
        assert deflated_sharpe(sr, 200, 50, kurtosis=9.0) < base

    def test_units_guard(self) -> None:
        """Per-trade Sharpe is small; annualised is not. Passing the wrong one
        makes the correction return 1.0 for anything, which is how a broken
        screener certifies noise."""
        assert deflated_sharpe(1.8, 200, 2300) == pytest.approx(1.0, abs=1e-6)
        assert deflated_sharpe(0.13, 200, 2300) < 0.2

    def test_degenerate_inputs(self) -> None:
        assert deflated_sharpe(0.5, 2, 10) == 0.0
        assert per_trade_sharpe([1.0, 1.0]) == 0.0
        assert per_trade_sharpe([1.0] * 50) == 0.0   # zero variance

    def test_moments(self) -> None:
        rng = random.Random(3)
        skew, kurt = _moments([rng.gauss(0, 1) for _ in range(5000)])
        assert abs(skew) < 0.2
        assert kurt == pytest.approx(3.0, abs=0.3)

        left, _ = _moments([-abs(rng.gauss(0, 1)) ** 2 for _ in range(5000)])
        assert left < -1.0


class TestPortfolio:
    def test_correlation_extremes(self) -> None:
        a = [0.01, -0.02, 0.03, -0.01, 0.02] * 20
        assert correlation(a, a) == pytest.approx(1.0)
        assert correlation(a, [-x for x in a]) == pytest.approx(-1.0)

    def test_correlation_of_independent_series_is_near_zero(self) -> None:
        rng = random.Random(4)
        a = [rng.gauss(0, 1) for _ in range(2000)]
        b = [rng.gauss(0, 1) for _ in range(2000)]
        assert abs(correlation(a, b)) < 0.1

    def test_effective_breadth_collapses_when_everything_moves_together(self) -> None:
        """Twenty symbols at rho 1.0 is one bet, not twenty."""
        names = [f"S{i}" for i in range(20)]
        perfect = {a: {b: 1.0 for b in names} for a in names}
        assert effective_breadth(perfect, names) == pytest.approx(1.0, abs=0.01)

        independent = {a: {b: (1.0 if a == b else 0.0) for b in names} for a in names}
        assert effective_breadth(independent, names) == pytest.approx(20.0, abs=0.01)

    def test_effective_breadth_never_exceeds_the_asset_count(self) -> None:
        names = ["A", "B", "C"]
        hedged = {a: {b: (1.0 if a == b else -0.9) for b in names} for a in names}
        assert effective_breadth(hedged, names) <= 3.0

    def test_clustering_groups_the_correlated_and_keeps_the_ranked_seed(self) -> None:
        names = ["BTC", "ETH", "SOL", "GOLD"]
        m = {a: {b: 0.0 for b in names} for a in names}
        for a in names:
            m[a][a] = 1.0
        for a, b in (("BTC", "ETH"), ("BTC", "SOL"), ("ETH", "SOL")):
            m[a][b] = m[b][a] = 0.9

        clusters = cluster_symbols(m, names, threshold=0.75)
        assert len(clusters) == 2
        crypto = next(c for c in clusters if "BTC" in c.members)
        assert set(crypto.members) == {"BTC", "ETH", "SOL"}
        assert crypto.representative == "BTC"      # first in the ranked order
        assert any(c.members == ("GOLD",) for c in clusters)

    def test_analyse_selects_one_per_cluster(self) -> None:
        base = make_candles(600, seed=11)
        frames = {
            "AAA": FeatureFrame("AAA", TF.H2, base),
            "BBB": FeatureFrame("BBB", TF.H2, base),          # identical
            "CCC": FeatureFrame("CCC", TF.H2, make_candles(600, seed=99)),
        }
        report = analyse(frames, ranked=["AAA", "BBB", "CCC"], threshold=0.75)
        assert "AAA" in report.selected
        assert "BBB" not in report.selected        # duplicate of AAA
        assert report.effective_breadth < 3.0
        assert report.concentration_multiplier > 1.0

    def test_max_positions_truncates_the_selection(self) -> None:
        frames = {
            f"S{i}": FeatureFrame(f"S{i}", TF.H2, make_candles(600, seed=i))
            for i in range(5)
        }
        report = analyse(frames, max_positions=2)
        assert len(report.selected) <= 2

    def test_summary_states_the_concentration(self) -> None:
        frames = {
            "A": FeatureFrame("A", TF.H2, make_candles(600, seed=1)),
            "B": FeatureFrame("B", TF.H2, make_candles(600, seed=2)),
        }
        text = analyse(frames).summary()
        assert "independent bets" in text


class TestPaperDelegatesMarketData:
    """A simulated account over real prices must still see real depth.

    Regression: the universe scanner behind a paper wrapper reported all 223
    markets as `no_data`, because PaperExchange delegated `candles` and `ticker`
    to its source but not `tickers` or `order_book`. Only the account is
    simulated; market data is market data.
    """

    class Source:
        name = "src"

        async def tickers(self):
            return [{"market": "BTCUSDT", "last": "100", "high": "104",
                     "low": "96", "value": "50000000"}]

        async def order_book(self, symbol, limit=50):
            from datetime import UTC, datetime
            return OrderBook(
                symbol=symbol,
                bids=(DepthLevel(Decimal("99"), Decimal("1000")),),
                asks=(DepthLevel(Decimal("100"), Decimal("1000")),),
                ts=datetime.now(UTC),
            )

    async def test_tickers_are_delegated(self) -> None:
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange(data_source=self.Source())  # type: ignore[arg-type]
        assert len(await ex.tickers()) == 1

    async def test_order_book_is_delegated(self) -> None:
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange(data_source=self.Source())  # type: ignore[arg-type]
        book = await ex.order_book("BTCUSDT")
        assert book is not None
        assert float(book.sweep(Decimal("5000"))) == pytest.approx(0.0, abs=1e-9)

    async def test_a_bare_paper_account_has_neither(self) -> None:
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange()
        assert await ex.tickers() == []
        assert await ex.order_book("BTCUSDT") is None


class TestPaperLedgerAgreesWithTheRunner:
    """Regression: two accounting systems that disagreed about the word "cash".

    Found in a live session — the paper ledger read −890,060,236 USDT while the
    runner's account read +96,087,532 on the same bot. The ledger was deducting
    posted margin from cash; the runner keeps margin derived from open positions
    and only moves cash on fees and realised PnL. Since the ledger is the side
    that vetoes an opening order, the bot spent its time refusing trades the
    risk engine had already approved.

    Margin now lives in the book on both sides, and an opening order is judged
    against capital not already backing a position.
    """

    async def _open(self, ex, symbol, side, qty, mark, lev="1"):
        from simin.core.types import OrderType

        ex.set_mark(symbol, Decimal(mark))
        await ex.set_leverage(symbol, Decimal(lev))
        return await ex.place_order(symbol, side, OrderType.MARKET, Decimal(qty))

    async def test_opening_does_not_drain_cash(self) -> None:
        """Cash moves on fees and realised PnL only — never on posted margin."""
        from simin.core.types import Side
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange(starting_balance=Decimal("100000"))
        await self._open(ex, "BTCUSDT", Side.BUY, "1", "50000", lev="5")

        free = (await ex.balances())["USDT"].free
        assert free == pytest.approx(Decimal("100000"), abs=Decimal("60"))
        assert ex.posted_margin() == pytest.approx(Decimal("10000"), abs=Decimal("30"))
        assert ex.free_capital() == pytest.approx(Decimal("90000"), abs=Decimal("60"))

    async def test_cash_cannot_run_away_across_many_positions(self) -> None:
        """The exact shape of the drift: several large leveraged opens used to
        drive the balance arbitrarily negative."""
        from simin.core.types import Side
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange(starting_balance=Decimal("1000000"))
        for i, sym in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT")):
            await self._open(ex, sym, Side.BUY if i % 2 else Side.SELL, "2", "50000", lev="6")

        free = (await ex.balances())["USDT"].free
        assert free > 0, "cash went negative purely from opening positions"
        assert free == pytest.approx(Decimal("1000000"), abs=Decimal("400"))
        assert ex.posted_margin() > 0

    async def test_free_capital_is_what_gates_an_open(self) -> None:
        """A balance that still looks healthy can already be fully committed."""
        from simin.core.types import OrderType, Side
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange(starting_balance=Decimal("10000"))
        await self._open(ex, "BTCUSDT", Side.BUY, "1", "9000", lev="1")
        assert ex.free_capital() < Decimal("1100")

        ex.set_mark("ETHUSDT", Decimal("9000"))
        with pytest.raises(Exception) as exc:
            await ex.place_order("ETHUSDT", Side.BUY, OrderType.MARKET, Decimal("1"))
        assert "free of margin" in str(exc.value)

    async def test_a_refused_order_leaves_no_phantom_position(self) -> None:
        from simin.core.types import OrderType, Side
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange(starting_balance=Decimal("100"))
        ex.set_mark("BTCUSDT", Decimal("50000"))
        with pytest.raises(Exception):
            await ex.place_order("BTCUSDT", Side.BUY, OrderType.MARKET, Decimal("1"))
        assert ex.position_book() == {}
        assert ex.posted_margin() == 0

    async def test_a_refused_add_leaves_the_original_position_intact(self) -> None:
        from simin.core.types import OrderType, Side
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange(starting_balance=Decimal("10000"))
        await self._open(ex, "BTCUSDT", Side.BUY, "1", "5000", lev="1")
        snapshot = ex.position_book()["BTCUSDT"]

        with pytest.raises(Exception):
            await ex.place_order("BTCUSDT", Side.BUY, OrderType.MARKET, Decimal("10"))
        assert ex.position_book()["BTCUSDT"] == snapshot

    async def test_round_trip_still_lands_on_the_right_pnl(self) -> None:
        from simin.core.types import OrderType, Side
        from simin.exchanges.paper import PaperExchange

        ex = PaperExchange(starting_balance=Decimal("100000"))
        await self._open(ex, "BTCUSDT", Side.BUY, "10", "100", lev="2")
        ex.set_mark("BTCUSDT", Decimal("110"))
        await ex.place_order("BTCUSDT", Side.SELL, OrderType.MARKET, Decimal("10"),
                             reduce_only=True)

        assert ex.position_book() == {}
        assert ex.posted_margin() == 0
        free = (await ex.balances())["USDT"].free
        assert free == pytest.approx(Decimal("100100"), abs=Decimal("5"))
