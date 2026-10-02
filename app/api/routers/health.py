"""Liveness, dependency readiness and Prometheus observation endpoints."""

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

from app.api.dependencies import get_health_service
from app.core.models import JobStatus
from app.core.services.health_service import HealthService

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(service: Annotated[HealthService, Depends(get_health_service)]) -> dict[str, str]:
    await service.check_readiness()
    return {"status": "ok"}


@router.get("/metrics")
async def metrics(service: Annotated[HealthService, Depends(get_health_service)]) -> Response:
    snapshot = await service.metrics_snapshot()
    # Each scrape reads durable totals. Registry isolation avoids duplicate series
    # and prevents counting the same persisted attempt again on repeated scrapes.
    registry = CollectorRegistry()
    jobs = Gauge("jobs_by_status", "Persisted jobs by status", ["status"], registry=registry)
    counts = cast(dict[str, int], snapshot["jobs_by_status"])
    for status in JobStatus:
        jobs.labels(status=status.value).set(counts.get(status.value, 0))
    for name, description in (
        ("llm_cost_usd_total", "Known billed provider cost in USD"),
        ("llm_errors_total", "Persisted unsuccessful provider attempts"),
        ("cache_hits_total", "Cache hits; persistence instrumentation is deferred"),
    ):
        metric = Counter(name, description, registry=registry)
        metric.inc(cast(float, snapshot[name]))
    return Response(generate_latest(registry), headers={"Content-Type": CONTENT_TYPE_LATEST})
