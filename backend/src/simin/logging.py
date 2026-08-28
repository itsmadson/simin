"""Structured logging.

`structlog` when available, a stdlib fallback when not, so the package imports
cleanly in a minimal environment without the logging call sites needing to care
which one they got.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

try:
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover - depends on the environment
    _HAS_STRUCTLOG = False


class _StdlibLogger:
    """Minimal structlog-shaped wrapper over the stdlib logger."""

    __slots__ = ("_log",)

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    @staticmethod
    def _fmt(event: str, kwargs: dict[str, Any]) -> str:
        if not kwargs:
            return event
        pairs = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
        return f"{event} {pairs}"

    def debug(self, event: str, **kw: Any) -> None:
        self._log.debug(self._fmt(event, kw))

    def info(self, event: str, **kw: Any) -> None:
        self._log.info(self._fmt(event, kw))

    def warning(self, event: str, **kw: Any) -> None:
        self._log.warning(self._fmt(event, kw))

    def error(self, event: str, **kw: Any) -> None:
        self._log.error(self._fmt(event, kw))

    def exception(self, event: str, **kw: Any) -> None:
        self._log.exception(self._fmt(event, kw))


def configure(level: str = "INFO", json_output: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, level, logging.INFO)
    )
    if not _HAS_STRUCTLOG:
        return
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level, logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return _StdlibLogger(name)
