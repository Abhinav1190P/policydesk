import asyncio
import random
import time
from collections import deque
from typing import Awaitable, Callable, TypeVar

from app.providers.base import ProviderError

T = TypeVar("T")


class MinuteLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.calls: deque[float] = deque()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self.per_minute <= 0:
            return
        async with self.lock:
            while True:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_minute:
                    self.calls.append(now)
                    return
                await asyncio.sleep(60 - (now - self.calls[0]) + 0.05)


async def with_retries(fn: Callable[[], Awaitable[T]], attempts: int = 3, base_delay: float = 0.5, max_delay: float = 8.0) -> T:
    last: Exception | None = None
    attempts = max(1, attempts)
    for i in range(attempts):
        try:
            return await fn()
        except ProviderError as e:
            last = e
            if not e.retryable or i == attempts - 1:
                raise
            if e.status == 429:
                delay = min(30.0, 6.0 * (2 ** i))
                await asyncio.sleep(delay + random.uniform(0, 1))
                continue
        delay = min(max_delay, base_delay * (2 ** i))
        await asyncio.sleep(random.uniform(0, delay))
    raise last if last else RuntimeError("retry loop exited")
