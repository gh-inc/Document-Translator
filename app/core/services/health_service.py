"""Dependency readiness and persisted observation, independent of the web layer."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from app.core.errors import ErrorCode, ServiceError


class HealthPersistence(Protocol):
    async def health_check(self) -> bool: ...

    async def stale_inflight_chunk_count(self, stale_before: datetime) -> int: ...

    async def metrics_snapshot(self) -> dict[str, object]: ...


class HealthService:
    def __init__(
        self,
        persistence: HealthPersistence,
        storage_probe: Callable[[], Awaitable[None]],
        *,
        stale_chunk_grace_seconds: int = 120,
    ) -> None:
        self._persistence = persistence
        self._storage_probe = storage_probe
        self._stale_chunk_grace = timedelta(seconds=stale_chunk_grace_seconds)

    async def check_readiness(self) -> None:
        try:
            if not await self._persistence.health_check():
                raise ServiceError(ErrorCode.NOT_READY, status_code=503)
            stale_before = datetime.now(UTC) - self._stale_chunk_grace
            if await self._persistence.stale_inflight_chunk_count(stale_before) > 0:
                raise ServiceError(ErrorCode.NOT_READY, status_code=503)
            await self._storage_probe()
        except Exception:
            raise ServiceError(ErrorCode.NOT_READY, status_code=503) from None

    async def metrics_snapshot(self) -> dict[str, object]:
        return await self._persistence.metrics_snapshot()
