"""Retry policy for worker tasks that call the translation provider."""

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.errors import ProviderError

T = TypeVar("T")


class Executor:
    """Run an async task with bounded retries for retryable provider errors."""

    def __init__(
        self,
        max_attempts: int,
        *,
        base_wait: float = 0.5,
        max_wait: float = 8.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if not math.isfinite(base_wait) or base_wait < 0:
            raise ValueError("base_wait must be a finite non-negative number")
        if not math.isfinite(max_wait) or max_wait < 0:
            raise ValueError("max_wait must be a finite non-negative number")
        self._max_attempts = max_attempts
        self._base_wait = base_wait
        self._max_wait = max_wait

    async def execute(self, task: Callable[[], Awaitable[T]]) -> T:
        """Execute ``task``, retrying only retryable ``ProviderError`` failures."""
        for attempt in range(self._max_attempts):
            try:
                return await task()
            except ProviderError as error:
                if not error.retryable or attempt + 1 >= self._max_attempts:
                    raise
                delay = min(
                    self._base_wait * (2**attempt) + random.uniform(0.0, self._base_wait),
                    self._max_wait,
                )
                await asyncio.sleep(delay)

        raise RuntimeError("executor finished without a result or provider error")
