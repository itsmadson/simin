"""Runtime configuration. Secrets come from the environment, never from the repo."""

from __future__ import annotations

import enum
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from simin.types import RunMode


class RiskProfile(enum.StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"


class RiskLimits(BaseSettings):
    """Hard limits. The engine rejects orders that violate any of these.

    The aggressive profile exists because the operator asked to hunt for the
    highest attainable return. It raises per-trade risk and exposure, but the
    ruin guards (drawdown halt, Kelly cap, correlation cap) are NOT removed —
    a profile that can reach zero equity is not a profile, it is a countdown.
    """

    risk_per_trade: Decimal
    max_open_positions: int
    max_exposure_per_asset: Decimal
    max_total_exposure: Decimal
    max_btc_beta_exposure: Decimal
    daily_loss_stop: Decimal
    weekly_loss_stop: Decimal
    dd_throttle_half: Decimal
    dd_throttle_quarter: Decimal
    dd_halt: Decimal
    max_consecutive_losses: int
    kelly_fraction: Decimal
    target_annual_vol: Decimal
    max_venue_exposure: Decimal = Decimal("0.50")


_PROFILES: dict[RiskProfile, RiskLimits] = {
    RiskProfile.CONSERVATIVE: RiskLimits(
        risk_per_trade=Decimal("0.0050"),
        max_open_positions=4,
        max_exposure_per_asset=Decimal("0.15"),
        max_total_exposure=Decimal("0.40"),
        max_btc_beta_exposure=Decimal("0.60"),
        daily_loss_stop=Decimal("0.02"),
        weekly_loss_stop=Decimal("0.05"),
        dd_throttle_half=Decimal("0.07"),
        dd_throttle_quarter=Decimal("0.12"),
        dd_halt=Decimal("0.15"),
        max_consecutive_losses=5,
        kelly_fraction=Decimal("0.25"),
        target_annual_vol=Decimal("0.15"),
    ),
    RiskProfile.BALANCED: RiskLimits(
        risk_per_trade=Decimal("0.0075"),
        max_open_positions=5,
        max_exposure_per_asset=Decimal("0.20"),
        max_total_exposure=Decimal("0.60"),
        max_btc_beta_exposure=Decimal("1.00"),
        daily_loss_stop=Decimal("0.03"),
        weekly_loss_stop=Decimal("0.07"),
        dd_throttle_half=Decimal("0.10"),
        dd_throttle_quarter=Decimal("0.15"),
        dd_halt=Decimal("0.20"),
        max_consecutive_losses=6,
        kelly_fraction=Decimal("0.50"),
        target_annual_vol=Decimal("0.25"),
    ),
    RiskProfile.AGGRESSIVE: RiskLimits(
        risk_per_trade=Decimal("0.0200"),
        max_open_positions=8,
        max_exposure_per_asset=Decimal("0.35"),
        max_total_exposure=Decimal("1.00"),
        max_btc_beta_exposure=Decimal("1.50"),
        daily_loss_stop=Decimal("0.06"),
        weekly_loss_stop=Decimal("0.12"),
        dd_throttle_half=Decimal("0.15"),
        dd_throttle_quarter=Decimal("0.22"),
        dd_halt=Decimal("0.30"),
        max_consecutive_losses=8,
        kelly_fraction=Decimal("0.75"),
        target_annual_vol=Decimal("0.60"),
    ),
}


def limits_for(profile: RiskProfile) -> RiskLimits:
    return _PROFILES[profile]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIMIN_", env_file=".env", extra="ignore", frozen=True
    )

    mode: RunMode = RunMode.PAPER
    risk_profile: RiskProfile = RiskProfile.BALANCED

    pg_dsn: str = "postgresql+asyncpg://simin:simin@localhost:5432/simin"
    redis_url: str = "redis://localhost:6379/0"

    public_data_base: str = "https://api.binance.com"
    parquet_dir: Path = Path("./data/parquet")
    http_timeout: float = 15.0

    paper_start_balance_irt: Decimal = Decimal("100000000")

    venue_plugin: str | None = None
    venue_api_key: SecretStr | None = None
    venue_api_secret: SecretStr | None = None

    log_level: str = "INFO"
    log_json: bool = True

    live_approval_token: SecretStr | None = Field(
        default=None,
        description="Required to run in LIVE mode; issued only after all Go/No-Go gates pass.",
    )

    @property
    def limits(self) -> RiskLimits:
        return limits_for(self.risk_profile)

    def assert_live_allowed(self) -> None:
        """LIVE is opt-in, token-gated, and never the default. See docs/03."""
        if self.mode is not RunMode.LIVE:
            return
        if self.live_approval_token is None:
            raise RuntimeError(
                "LIVE mode requires SIMIN_LIVE_APPROVAL_TOKEN, issued only after the "
                "Go/No-Go gates in docs/03-risk-and-validation.md all pass."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
