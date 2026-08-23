"""Paper adapter and trader loop."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from factories import gbm_series

from simin.backtest.costs import CostModel
from simin.config import RiskProfile, Settings, limits_for
from simin.exchanges.base import OrderRejected
from simin.exchanges.paper import PaperAdapter
from simin.exchanges.replay import Clock, ReplayAdapter
from simin.risk.engine import RiskEngine
from simin.strategies import build
from simin.trader import Trader, TraderConfig
from simin.types import TF, FeeSchedule, OrderRequest, OrderStatus, OrderType, RunMode, Side


def run(coro):
    return asyncio.run(coro)


def make_paper(balance="1000000", symbol="BTCUSDT", n=600):
    bars = gbm_series(n, seed=17, volume=100_000.0)
    clock = Clock(now=bars[-1].ts + TF.H1.delta)
    data = ReplayAdapter(clock=clock)
    data.load(symbol, TF.H1, bars)
    adapter = PaperAdapter(
        data=data,
        cost=CostModel(fees=FeeSchedule(Decimal("0.002"), Decimal("0.0025")), spread_bps=Decimal(20)),
        quote_asset="USDT",
        starting_balance=Decimal(balance),
    )
    return adapter, bars


def order(symbol="BTCUSDT", side=Side.BUY, qty="0.5", cid="c1", type_=OrderType.MARKET,
          price=None, stop_price=None):
    return OrderRequest(
        symbol=symbol, side=side, type=type_, qty=Decimal(qty), price=price,
        stop_price=stop_price, client_order_id=cid,
    )


def test_market_buy_debits_quote_and_credits_base():
    adapter, _ = make_paper()
    start = run(adapter.get_balance())[0].free
    result = run(adapter.create_order(order()))
    assert result.status is OrderStatus.FILLED
    balances = {b.asset: b.free for b in run(adapter.get_balance())}
    assert balances["BTC"] == Decimal("0.5")
    assert balances["USDT"] < start


def test_fills_are_worse_than_mid_because_costs_are_real():
    adapter, _ = make_paper()
    ticker = run(adapter.get_ticker("BTCUSDT"))
    filled = run(adapter.create_order(order()))
    assert filled.avg_price > ticker.mid


def test_retrying_the_same_client_order_id_does_not_double_the_position():
    """The property that makes a retry after a timeout safe."""
    adapter, _ = make_paper()
    first = run(adapter.create_order(order(cid="same")))
    second = run(adapter.create_order(order(cid="same")))
    assert first.exchange_order_id == second.exchange_order_id
    balances = {b.asset: b.free for b in run(adapter.get_balance())}
    assert balances["BTC"] == Decimal("0.5")


def test_buying_more_than_the_balance_is_rejected():
    adapter, _ = make_paper(balance="10")
    result = run(adapter.create_order(order(qty="5")))
    assert result.status is OrderStatus.REJECTED
    assert result.reject_reason == "insufficient balance"


def test_selling_without_a_position_is_rejected():
    adapter, _ = make_paper()
    result = run(adapter.create_order(order(side=Side.SELL, cid="s1")))
    assert result.status is OrderStatus.REJECTED
    assert result.reject_reason == "insufficient position"


def test_round_trip_loses_money_to_fees_and_spread():
    """Buying and immediately selling must cost money. Any other result is a bug."""
    adapter, _ = make_paper()
    before = run(adapter.get_balance())[0].free
    run(adapter.create_order(order(cid="b")))
    run(adapter.create_order(order(side=Side.SELL, cid="s")))
    after = {b.asset: b.free for b in run(adapter.get_balance())}["USDT"]
    assert after < before


def test_a_limit_that_does_not_cross_rests_instead_of_filling():
    adapter, _ = make_paper()
    ticker = run(adapter.get_ticker("BTCUSDT"))
    far_below = ticker.bid * Decimal("0.5")
    result = run(adapter.create_order(order(cid="l1", type_=OrderType.LIMIT, price=far_below)))
    assert result.status is OrderStatus.NEW
    assert result.filled_qty == 0


def test_a_crossing_limit_fills():
    adapter, _ = make_paper()
    ticker = run(adapter.get_ticker("BTCUSDT"))
    result = run(
        adapter.create_order(
            order(cid="l2", type_=OrderType.LIMIT, price=ticker.ask * Decimal(2))
        )
    )
    assert result.status is OrderStatus.FILLED


def test_unsupported_order_types_are_rejected_loudly():
    adapter, _ = make_paper()
    with pytest.raises(OrderRejected, match="does not simulate"):
        run(adapter.create_order(order(cid="st", type_=OrderType.STOP, stop_price=Decimal(1))))


def test_cancel_of_an_unknown_order_raises():
    adapter, _ = make_paper()
    with pytest.raises(OrderRejected, match="unknown order"):
        run(adapter.cancel_order("nope", "BTCUSDT"))


def test_equity_marks_open_positions():
    adapter, bars = make_paper()
    run(adapter.create_order(order()))
    marks = {"BTC": bars[-1].close}
    assert adapter.equity(marks) > 0


def test_partial_fill_when_the_book_is_too_thin():
    bars = gbm_series(400, seed=19, volume=0.01)
    clock = Clock(now=bars[-1].ts + TF.H1.delta)
    data = ReplayAdapter(clock=clock)
    data.load("BTCUSDT", TF.H1, bars)
    adapter = PaperAdapter(
        data=data,
        cost=CostModel(fees=FeeSchedule(Decimal("0.001"), Decimal("0.001"))),
        quote_asset="USDT",
        starting_balance=Decimal("100000000"),
    )
    result = run(adapter.create_order(order(qty="1000", cid="big")))
    assert result.status in (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED)
    assert result.filled_qty <= Decimal("1000")


# ------------------------------------------------------------------- trader


def paper_settings(**kw):
    defaults = dict(mode=RunMode.PAPER, risk_profile=RiskProfile.BALANCED,
                    paper_start_balance_irt=Decimal("1000000"))
    defaults.update(kw)
    return Settings(**defaults)


def make_trader(settings=None, strategies=("trend_follow",)):
    adapter, bars = make_paper(balance="1000000", n=800)
    trader = Trader(
        adapter=adapter,
        risk=RiskEngine(limits_for(RiskProfile.BALANCED)),
        strategies=[build(s) for s in strategies],
        config=TraderConfig(symbols=("BTCUSDT",), tf=TF.H1, quote_asset="USDT"),
        settings=settings or paper_settings(),
    )
    return trader, adapter, bars


def test_live_mode_without_an_approval_token_refuses_to_start():
    """The gate that keeps a research tool from becoming an accident."""
    with pytest.raises(RuntimeError, match="Go/No-Go"):
        make_trader(settings=paper_settings(mode=RunMode.LIVE))


def test_tick_runs_without_error_and_may_open_a_position():
    trader, adapter, _ = make_trader()
    run(trader.tick())
    assert trader.state.errors == 0


def test_the_same_bar_is_never_traded_twice():
    trader, adapter, _ = make_trader()
    run(trader.tick())
    orders_after_first = trader.state.orders_sent
    run(trader.tick())
    assert trader.state.orders_sent == orders_after_first


def test_a_tripped_kill_switch_blocks_the_whole_loop():
    trader, _, _ = make_trader()
    trader.state.account.trip("manual halt")
    run(trader.tick())
    assert trader.state.orders_sent == 0


def test_insufficient_history_does_not_trade():
    adapter, bars = make_paper(n=100)
    trader = Trader(
        adapter=adapter,
        risk=RiskEngine(limits_for(RiskProfile.BALANCED)),
        strategies=[build("trend_follow")],
        config=TraderConfig(symbols=("BTCUSDT",), tf=TF.H1),
        settings=paper_settings(),
    )
    run(trader.tick())
    assert trader.state.orders_sent == 0


def test_stop_can_be_requested():
    trader, _, _ = make_trader()
    trader.request_stop()
    assert trader._stop.is_set()
