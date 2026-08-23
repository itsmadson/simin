"""Structured logging.

structlog when available (JSON in production, pretty in a terminal), with a
stdlib fallback so that the domain layer and its tests stay runnable in a bare
environment. Credentials are redacted in both paths — a log sink is the most
common place an API key escapes.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Protocol

_SECRET_KEYS = {
    "api_key",
    "api_secret",
    "secret",
    "password",
    "token",
    "authorization",
    "live_approval_token",
}


def redact(event: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***redacted***" if k.lower() in _SECRET_KEYS else v) for k, v in event.items()}


class Logger(Protocol):
    def debug(self, event: str, **kw: Any) -> None: ...
    def info(self, event: str, **kw: Any) -> None: ...
    def warning(self, event: str, **kw: Any) -> None: ...
    def error(self, event: str, **kw: Any) -> None: ...
    def exception(self, event: str, **kw: Any) -> None: ...


try:  # pragma: no cover - import-time branch
    import structlog

    _HAVE_STRUCTLOG = True
except ImportError:  # pragma: no cover
    _HAVE_STRUCTLOG = False


class _StdlibLogger:
    """Minimal key-value logger used when structlog is not installed."""

    def __init__(self, name: str, *, as_json: bool = True) -> None:
        self._log = logging.getLogger(name)
        self._json = as_json

    def _emit(self, level: int, event: str, kw: dict[str, Any]) -> None:
        payload = redact(kw)
        if self._json:
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": logging.getLevelName(level).lower(),
                "event": event,
                **payload,
            }
            self._log.log(level, json.dumps(record, default=str))
        else:
            extras = " ".join(f"{k}={v}" for k, v in payload.items())
            self._log.log(level, f"{event} {extras}".rstrip())

    def debug(self, event: str, **kw: Any) -> None:
        self._emit(logging.DEBUG, event, kw)

    def info(self, event: str, **kw: Any) -> None:
        self._emit(logging.INFO, event, kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._emit(logging.WARNING, event, kw)

    def error(self, event: str, **kw: Any) -> None:
        self._emit(logging.ERROR, event, kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._emit(logging.ERROR, event, {**kw, "exc_info": True})


_AS_JSON = True


def configure_logging(level: str = "INFO", *, as_json: bool = True) -> None:
    global _AS_JSON
    _AS_JSON = as_json
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=getattr(logging, level))
    if not _HAVE_STRUCTLOG:
        return

    def _redact_processor(_l: Any, _n: str, event: dict[str, Any]) -> dict[str, Any]:
        return redact(event)

    renderer: Any = (
        structlog.processors.JSONRenderer() if as_json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Logger:
    if _HAVE_STRUCTLOG:
        return structlog.get_logger(name)  # type: ignore[no-any-return]
    return _StdlibLogger(name, as_json=_AS_JSON)
