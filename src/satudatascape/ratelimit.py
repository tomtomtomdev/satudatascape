"""Token-bucket rate limiting with an injectable clock (PLAN: injectable time)."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[Any]]


class TokenBucket:
    """Hands out one slot every `1/rps` seconds (burst of one); callers wait for their slot."""

    def __init__(
        self, rps: float, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        self._interval = 1.0 / rps
        self._clock = clock
        self._sleep = sleep
        self._next_slot: float | None = None

    async def acquire(self) -> None:
        now = self._clock()
        slot = now if self._next_slot is None else max(now, self._next_slot)
        self._next_slot = slot + self._interval
        if slot > now:
            await self._sleep(slot - now)
