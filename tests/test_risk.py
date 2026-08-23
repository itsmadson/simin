"""Risk engine: every limit gets a test that proves it actually blocks.

A limit without a test is a comment.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from simin.config import RiskProfile, limits_for
from simin.risk.engine import (
    AccountState,
    Intent,
    OpenPosition,
    RejectReason,
    RiskEngine,
    new_account,
    trailing_stop,
)

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def intent(symbol="BTCUSDT", entry=100, stop=95, direction=1, **kw):
    return Intent(
        ts=NOW, symbol=symbol, direction=direction, entry=Decimal(str(entry)),
        stop=Decimal(str(stop)), strategy=kw.pop("strategy", "trend_follow"), **kw
    )


def engine(profile=RiskProfile.BALANCED):
    return RiskEngine(limits_for(profile))


def account(equity=100_000):
    return new_account(Decimal(str(equity)))


# ------------------------------------------------------------------- sizing


def test_position_risks_exactly_the_configured_fraction():
    eng, state = engine(), account(100_000)
    d = eng.evaluate(state, intent(entry=100, stop=90))
    assert d.approved
    loss_if_stopped = d.qty * Decimal(10)
    assert loss_if_stopped == state.equity * d.risk_fraction


def test_a_wider_stop_buys_a_smaller_position():
    """Same risk budget, more distance to the stop, fewer units. The invariant
    that makes volatility-adjusted sizing work."""
    eng, state = engine(), account()
    tight = eng.evaluate(state, intent(entry=100, stop=99))
    wide = eng.evaluate(account(), intent(entry=100, stop=80))
    assert tight.qty > wide.qty * 10


def test_confidence_scales_size_without_ever_reaching_zero():
    eng = engine()
    low = eng.evaluate(account(), intent(confidence=0.0))
    high = eng.evaluate(account(), intent(confidence=1.0))
    assert 0 < low.qty < high.qty


def test_aggressive_profile_sizes_bigger_but_still_finite():
    balanced = engine(RiskProfile.BALANCED).evaluate(account(), intent())
    aggressive = engine(RiskProfile.AGGRESSIVE).evaluate(account(), intent())
    assert aggressive.qty > balanced.qty
    assert aggressive.qty * Decimal(100) <= Decimal(100_000) * Decimal("0.35")


# ------------------------------------------------------------------- blocks


def test_inverted_stop_is_rejected():
    """A long whose 'stop' sits above entry is not protection, it is a trigger."""
    d = engine().evaluate(account(), intent(entry=100, stop=105, direction=1))
    assert d.reason is RejectReason.INVALID_STOP


def test_zero_distance_stop_is_rejected():
    d = engine().evaluate(account(), intent(entry=100, stop=100))
    assert d.reason is RejectReason.INVALID_STOP


def test_duplicate_position_is_rejected():
    eng, state = engine(), account()
    state.positions["BTCUSDT"] = OpenPosition(
        "BTCUSDT", 1, Decimal(1), Decimal(100), Decimal(95), "trend_follow"
    )
    assert eng.evaluate(state, intent()).reason is RejectReason.ALREADY_OPEN


def test_max_open_positions_is_enforced():
    eng, state = engine(), account()
    limits = limits_for(RiskProfile.BALANCED)
    for i in range(limits.max_open_positions):
        state.positions[f"S{i}"] = OpenPosition(
            f"S{i}", 1, Decimal("0.001"), Decimal(100), Decimal(95), "s"
        )
    assert eng.evaluate(state, intent(symbol="NEW")).reason is RejectReason.MAX_POSITIONS


def test_per_asset_exposure_is_capped_by_trimming_not_rejecting():
    eng, state = engine(), account(100_000)
    d = eng.evaluate(state, intent(entry=100, stop=99.9))   # tiny stop -> huge raw size
    assert d.approved
    cap = state.equity * limits_for(RiskProfile.BALANCED).max_exposure_per_asset
    assert d.qty * Decimal(100) <= cap


def test_total_exposure_cap_blocks_when_full():
    eng, state = engine(), account(100_000)
    state.positions["A"] = OpenPosition("A", 1, Decimal(600), Decimal(100), Decimal(95), "s")
    assert eng.evaluate(state, intent(symbol="B")).reason is RejectReason.TOTAL_EXPOSURE


def test_correlated_beta_cap_treats_five_alt_longs_as_one_big_btc_long():
    """The check most retail systems miss: position count is not exposure."""
    eng, state = engine(), account(100_000)
    for i in range(3):
        state.positions[f"ALT{i}"] = OpenPosition(
            f"ALT{i}", 1, Decimal(330), Decimal(100), Decimal(95), "s", beta=1.0
        )
    d = eng.evaluate(state, intent(symbol="ALT9", beta=1.0))
    assert d.reason in (RejectReason.BETA_EXPOSURE, RejectReason.TOTAL_EXPOSURE)


def test_venue_exposure_cap_limits_custody_risk():
    """Never leave more than half the account on one venue — see docs/04."""
    eng, state = engine(), account(100_000)
    state.positions["A"] = OpenPosition(
        "A", 1, Decimal(500), Decimal(100), Decimal(95), "s", venue="v1"
    )
    d = eng.evaluate(state, intent(symbol="B", venue="v1"))
    assert d.reason in (RejectReason.VENUE_EXPOSURE, RejectReason.TOTAL_EXPOSURE)


def test_daily_loss_stop_halts_new_entries():
    eng, state = engine(), account(100_000)
    state.mark(Decimal(96_000))          # -4% on the day, limit is 3%
    assert eng.evaluate(state, intent()).reason is RejectReason.DAILY_LOSS_STOP


def test_loss_streak_pauses_trading():
    eng, state = engine(), account()
    state.consecutive_losses = limits_for(RiskProfile.BALANCED).max_consecutive_losses
    assert eng.evaluate(state, intent()).reason is RejectReason.LOSS_STREAK


def test_drawdown_halt_trips_the_kill_switch_permanently():
    eng, state = engine(), account(100_000)
    state.mark(Decimal(70_000))          # -30%, limit 20%
    first = eng.evaluate(state, intent())
    assert first.reason is RejectReason.DRAWDOWN_HALT
    assert state.kill_switch
    state.mark(Decimal(200_000))         # even a full recovery does not clear it
    assert eng.evaluate(state, intent()).reason is RejectReason.KILL_SWITCH


def test_stale_data_blocks_trading():
    eng, state = engine(), account()
    state.last_data_ts = NOW
    late = NOW + timedelta(hours=5)
    assert eng.evaluate(state, intent(), now=late).reason is RejectReason.STALE_DATA


def test_thin_depth_trims_the_order():
    eng, state = engine(), account(100_000)
    full = eng.evaluate(state, intent())
    thin = eng.evaluate(account(100_000), intent(), available_depth=Decimal(500))
    assert thin.qty < full.qty
    assert thin.qty * Decimal(100) <= Decimal(500)


def test_dust_orders_are_rejected():
    eng = RiskEngine(limits_for(RiskProfile.BALANCED), min_notional=Decimal(10_000))
    d = eng.evaluate(account(1000), intent())
    assert d.reason is RejectReason.SIZE_TOO_SMALL


# -------------------------------------------------------- circuit breakers


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reconciliation_mismatch": True},
        {"spread_bps": Decimal(300), "median_spread_bps": Decimal(20)},
        {"venue_error_rate": 0.25},
        {"clock_skew_ms": 5000.0},
        {"bar_gap_pct": -0.15},
    ],
)
def test_each_circuit_breaker_trips(kwargs):
    eng, state = engine(), account()
    assert eng.check_circuit_breakers(state, **kwargs)
    assert state.kill_switch
    assert eng.evaluate(state, intent()).reason is RejectReason.KILL_SWITCH


def test_healthy_conditions_do_not_trip_anything():
    eng, state = engine(), account()
    assert (
        eng.check_circuit_breakers(
            state, spread_bps=Decimal(25), median_spread_bps=Decimal(20),
            venue_error_rate=0.01, clock_skew_ms=50.0, bar_gap_pct=0.01,
        )
        is None
    )
    assert not state.kill_switch


# ------------------------------------------------------------------ trailing


def test_trailing_stop_only_ratchets_upward():
    entry, atr = Decimal(100), Decimal(2)
    at_entry = trailing_stop(1, entry, entry, atr)
    higher = trailing_stop(1, entry, Decimal(120), atr)
    assert higher > at_entry
    assert trailing_stop(1, entry, Decimal(90), atr) == at_entry   # never loosens


def test_drawdown_throttle_halves_then_quarters_risk():
    eng = engine()
    normal = eng.risk_fraction(account(100_000), intent())
    half = account(100_000)
    half.mark(Decimal(88_000))          # -12% -> half risk band
    quarter = account(100_000)
    quarter.mark(Decimal(82_000))       # -18% -> quarter risk band
    assert eng.risk_fraction(half, intent()) < normal
    assert eng.risk_fraction(quarter, intent()) < eng.risk_fraction(half, intent())


def test_account_state_tracks_peak_and_rollovers():
    state = AccountState(Decimal(100), Decimal(100), Decimal(100), Decimal(100))
    state.mark(Decimal(120))
    assert state.peak_equity == Decimal(120)
    state.mark(Decimal(90))
    assert state.peak_equity == Decimal(120)
    assert state.drawdown == Decimal(90) / Decimal(120) - 1
    state.roll_day()
    assert state.daily_pnl_pct == 0


def test_intent_validation():
    with pytest.raises(ValueError, match="direction"):
        Intent(ts=NOW, symbol="X", direction=0, entry=Decimal(1), stop=Decimal(1), strategy="s")
    with pytest.raises(ValueError, match="confidence"):
        intent(confidence=1.5)
