"""Token-bucket limiter and retry policy, applied once for every adapter.

Written here rather than per-adapter so that a new venue cannot forget it: an
adapter that hammers a venue gets the account banned, and a retry without an
idempotency key gets you two positions where you wanted one.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from simin.exchanges.base import RateLimited, VenueError

T = TypeVar("T")


@dataclass(slots=True)
class TokenBucket:
    """``capacity`` requests per ``per_seconds``, refilled continuously."""

    capacity: float
    per_seconds: float
    _tokens: float = 0.0
    _last: float = 0.0

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.per_seconds <= 0:
            raise ValueError("capacity and per_seconds must be positive")
        self._tokens = self.capacity
        self._last = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.capacity / self.per_seconds)

    async def acquire(self, cost: float = 1.0) -> None:
        if cost > self.capacity:
            raise ValueError("cost exceeds bucket capacity")
        while True:
            now = time.monotonic()
            self._refill(now)
            if self._tokens >= cost:
                self._tokens -= cost
                return
            deficit = cost - self._tokens
            await asyncio.sleep(deficit * self.per_seconds / self.capacity)


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.3,
) -> T:
    """Retry only errors flagged retryable; honour server-supplied backoff.

    Non-retryable errors (a rejected order, a bad signature) propagate immediately —
    retrying those turns one problem into several.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return await fn()
        except VenueError as exc:
            if not exc.retryable or attempt == attempts - 1:
                raise
            last = exc
            delay = min(max_delay, base_delay * 2**attempt)
            if isinstance(exc, RateLimited) and exc.retry_after:
                delay = max(delay, exc.retry_after)
            await asyncio.sleep(delay * (1 + random.uniform(-jitter, jitter)))
    raise last if last else RuntimeError("unreachable")


@dataclass(slots=True)
class CircuitBreaker:
    """Stop calling a venue that is clearly broken, and probe it occasionally.

    Without this, a venue outage becomes a retry storm that costs you the rate
    limit budget you will need the moment it recovers.
    """

    failure_threshold: int = 5
    reset_after: float = 30.0
    _failures: int = 0
    _opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.reset_after:
            self._opened_at = None
            self._failures = 0
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()
