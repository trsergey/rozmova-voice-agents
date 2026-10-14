"""Retry policy shared by the provider adapters (ADR 0003, SDD "LLM-шар").

A failed attempt is retried with exponential backoff only while nothing has been yielded
yet. Once the first event has reached the caller the text may already be on the client,
so a failure is re-raised as is and the gateway reports it as ``error{retryable}``.
"""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass, field

from rozmova_engine.providers.base import ProviderError, StreamEvent


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """``attempts`` in total; the delay before retry ``n`` (1-based) is
    ``min(base_delay * 2 ** (n - 1), max_delay)``. ``sleep`` is injectable for tests."""

    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 4.0
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, compare=False)

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be at least 1")

    def delay(self, retry: int) -> float:
        return min(self.base_delay * 2.0 ** (retry - 1), self.max_delay)


async def stream_with_retries(
    attempt: Callable[[], AsyncGenerator[StreamEvent]], policy: RetryPolicy
) -> AsyncGenerator[StreamEvent]:
    """Run ``attempt()`` and re-run it on a retryable ``ProviderError`` before the first event.

    ``attempt`` must map vendor errors to ``ProviderError``; anything else propagates
    untouched, including ``CancelledError``. Each attempt is closed before the next one.
    """
    for number in range(1, policy.attempts + 1):
        started = False
        try:
            async with aclosing(attempt()) as events:
                async for event in events:
                    started = True
                    yield event
            return
        except ProviderError as err:
            if started or not err.retryable or number == policy.attempts:
                raise
        await policy.sleep(policy.delay(number))
