import asyncio
import time

import pytest

from simin.exchanges.base import OrderRejected, RateLimited, VenueUnavailable
from simin.exchanges.ratelimit import CircuitBreaker, TokenBucket, with_retry


def test_bucket_allows_burst_up_to_capacity():
    bucket = TokenBucket(capacity=5, per_seconds=60)
    async def burst():
        await asyncio.gather(*(bucket.acquire() for _ in range(5)))

    started = time.monotonic()
    asyncio.run(burst())
    assert time.monotonic() - started < 0.05


def test_bucket_throttles_beyond_capacity():
    bucket = TokenBucket(capacity=2, per_seconds=0.2)

    async def drain():
        for _ in range(4):
            await bucket.acquire()

    started = time.monotonic()
    asyncio.run(drain())
    assert time.monotonic() - started >= 0.15


def test_bucket_rejects_impossible_cost():
    bucket = TokenBucket(capacity=2, per_seconds=1)
    with pytest.raises(ValueError, match="exceeds bucket capacity"):
        asyncio.run(bucket.acquire(5))


def test_retry_recovers_from_a_retryable_failure():
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise VenueUnavailable("boom")
        return "ok"

    assert asyncio.run(with_retry(flaky, base_delay=0.001)) == "ok"
    assert calls["n"] == 3


def test_rejected_orders_are_never_retried():
    """Retrying a rejection is how one intended position becomes three."""
    calls = {"n": 0}

    async def rejected():
        calls["n"] += 1
        raise OrderRejected("insufficient balance")

    with pytest.raises(OrderRejected):
        asyncio.run(with_retry(rejected, base_delay=0.001))
    assert calls["n"] == 1


def test_retry_honours_server_supplied_backoff():
    async def limited():
        raise RateLimited("slow down", retry_after=0.05)

    started = time.monotonic()
    with pytest.raises(RateLimited):
        asyncio.run(with_retry(limited, attempts=2, base_delay=0.001))
    assert time.monotonic() - started >= 0.03


def test_circuit_breaker_opens_then_recovers():
    breaker = CircuitBreaker(failure_threshold=2, reset_after=0.05)
    assert not breaker.is_open
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.is_open
    time.sleep(0.06)
    assert not breaker.is_open


def test_success_resets_the_failure_count():
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    assert not breaker.is_open
