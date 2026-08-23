from decimal import Decimal

import pytest

from simin.config import RiskProfile, Settings, limits_for
from simin.exchanges.venues import profile
from simin.types import RunMode


def test_aggressive_profile_raises_risk_but_keeps_ruin_guards():
    aggressive = limits_for(RiskProfile.AGGRESSIVE)
    balanced = limits_for(RiskProfile.BALANCED)
    assert aggressive.risk_per_trade > balanced.risk_per_trade
    assert aggressive.max_total_exposure > balanced.max_total_exposure
    # the guards that prevent a zero: still present, still finite
    assert 0 < aggressive.dd_halt < Decimal(1)
    assert aggressive.kelly_fraction <= Decimal(1)   # never above full Kelly
    assert aggressive.daily_loss_stop > 0
    assert aggressive.max_venue_exposure <= Decimal("0.50")


def test_every_profile_halts_before_total_loss():
    for prof in RiskProfile:
        limits = limits_for(prof)
        assert limits.dd_halt < Decimal("0.5"), f"{prof} would ride a 50% drawdown"
        assert limits.dd_throttle_half < limits.dd_throttle_quarter < limits.dd_halt


def test_live_mode_requires_an_approval_token():
    settings = Settings(mode=RunMode.LIVE, live_approval_token=None)
    with pytest.raises(RuntimeError, match="Go/No-Go"):
        settings.assert_live_allowed()


def test_paper_is_the_default_mode():
    assert Settings().mode is RunMode.PAPER


def test_local_venue_round_trip_cost_excludes_scalping():
    """~1.1% round trip means sub-1h strategies are dead on arrival."""
    cost = profile("local_irt_generic").round_trip_cost()
    assert cost > Decimal("0.01")


def test_unknown_venue_profile_fails_loudly():
    with pytest.raises(KeyError, match="unknown venue profile"):
        profile("definitely_not_a_venue")
