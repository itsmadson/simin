"""Settings. Everything has a working default; nothing secret has one.

`SIMIN_MODE` is the single most consequential setting in the file. It defaults
to `lab`, and `real` requires credentials plus an explicit acknowledgement, so
that no accident, typo, or forgotten env file can start a bot that spends money.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from simin.core.types import Mode


def _env(key: str, default: str = "") -> str:
    return os.environ.get(f"SIMIN_{key}", default).strip()


def _flag(key: str, default: bool = False) -> bool:
    raw = _env(key, "1" if default else "0").lower()
    return raw in ("1", "true", "yes", "on")


def _dec(key: str, default: str) -> Decimal:
    return Decimal(_env(key, default))


def _int(key: str, default: int) -> int:
    return int(_env(key, str(default)))


@dataclass(frozen=True, slots=True)
class VenueCredentials:
    key: str = ""
    secret: str = ""
    passphrase: str = ""
    base_url: str = ""

    @property
    def present(self) -> bool:
        return bool(self.key and self.secret)


@dataclass(frozen=True, slots=True)
class Settings:
    mode: Mode
    #: The dial position the bot starts at. The user changes it from the UI.
    risk_level: int
    venue: str
    symbols: tuple[str, ...]
    quote: str
    starting_equity: Decimal

    data_dir: Path
    db_url: str
    api_host: str
    api_port: int
    cors_origins: tuple[str, ...]

    #: Real mode refuses to start unless this is explicitly true. It is the
    #: human-in-the-loop step; there is deliberately no way to default it on.
    real_mode_acknowledged: bool
    #: Hard ceiling on capital the bot may ever deploy, independent of the dial.
    max_capital: Decimal
    #: Global kill switch. When set, no order of any kind leaves the process.
    trading_frozen: bool

    credentials: dict[str, VenueCredentials] = field(default_factory=dict)

    poll_seconds: int = 15
    warmup_bars: int = 300
    log_level: str = "INFO"
    log_json: bool = False

    @property
    def is_real(self) -> bool:
        return self.mode is Mode.REAL

    def creds(self, venue: str) -> VenueCredentials:
        return self.credentials.get(venue, VenueCredentials())

    def validate_for_start(self) -> list[str]:
        """Reasons the bot must not start. Empty list means it may."""
        problems: list[str] = []
        if not 1 <= self.risk_level <= 10:
            problems.append(f"risk level {self.risk_level} is outside 1..10")
        if not self.symbols:
            problems.append("no symbols configured")
        if self.starting_equity <= 0:
            problems.append("starting equity must be positive")
        if self.is_real:
            if not self.real_mode_acknowledged:
                problems.append(
                    "real mode requires SIMIN_REAL_MODE_ACKNOWLEDGED=1 — this is the "
                    "deliberate human confirmation step and has no default"
                )
            if not self.creds(self.venue).present:
                problems.append(f"real mode on {self.venue} has no API credentials")
            if self.max_capital <= 0:
                problems.append("real mode requires SIMIN_MAX_CAPITAL > 0")
        return problems


def _load_credentials() -> dict[str, VenueCredentials]:
    """Credentials are read per-venue from SIMIN_<VENUE>_KEY / _SECRET / _PASSPHRASE.

    Never logged, never serialised into an API response, never written to disk.
    """
    creds: dict[str, VenueCredentials] = {}
    for venue in ("coinex", "nobitex", "wallex"):
        up = venue.upper()
        c = VenueCredentials(
            key=_env(f"{up}_KEY"),
            secret=_env(f"{up}_SECRET"),
            passphrase=_env(f"{up}_PASSPHRASE"),
            base_url=_env(f"{up}_BASE_URL"),
        )
        if c.present or c.base_url:
            creds[venue] = c
    return creds


@lru_cache(maxsize=1)
def settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", "./data")).expanduser().resolve()
    symbols = tuple(s for s in _env("SYMBOLS", "BTCUSDT ETHUSDT SOLUSDT").split() if s)
    return Settings(
        mode=Mode(_env("MODE", "lab").lower()),
        risk_level=_int("RISK_LEVEL", 4),
        venue=_env("VENUE", "paper").lower(),
        symbols=symbols,
        quote=_env("QUOTE", "USDT").upper(),
        starting_equity=_dec("STARTING_EQUITY", "10000"),
        data_dir=data_dir,
        db_url=_env("DB_URL", f"sqlite+aiosqlite:///{data_dir / 'simin.db'}"),
        api_host=_env("API_HOST", "0.0.0.0"),
        api_port=_int("API_PORT", 8000),
        cors_origins=tuple(
            o for o in _env("CORS_ORIGINS", "http://localhost:3000").split(",") if o
        ),
        real_mode_acknowledged=_flag("REAL_MODE_ACKNOWLEDGED", False),
        max_capital=_dec("MAX_CAPITAL", "0"),
        trading_frozen=_flag("TRADING_FROZEN", False),
        credentials=_load_credentials(),
        poll_seconds=_int("POLL_SECONDS", 15),
        warmup_bars=_int("WARMUP_BARS", 300),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        log_json=_flag("LOG_JSON", False),
    )


def reset_settings_cache() -> None:
    """Tests mutate the environment; production never calls this."""
    settings.cache_clear()
