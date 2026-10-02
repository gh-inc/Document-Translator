"""Dependency readiness and persisted observation, independent of the web layer."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from app.core.errors import ErrorCode, ServiceError


class HealthPersistence(Protocol):
    async def health_check(self) -> bool: ...

    async def metrics_snapshot(self) -> dict[str, object]: ...


class HealthService:
    def __init__(
        self, persistence: HealthPersistence, storage_probe: Callable[[], Awaitable[None]]
    ) -> None:
        self._persistence = persistence
        self._storage_probe = storage_probe

    async def check_readiness(self) -> None:
        try:
            if not await self._persistence.health_check():
                raise ServiceError(ErrorCode.NOT_READY, status_code=503)
            await self._storage_probe()
        except Exception:
            raise ServiceError(ErrorCode.NOT_READY, status_code=503) from None

    async def metrics_snapshot(self) -> dict[str, object]:
        return await self._persistence.metrics_snapshot()
