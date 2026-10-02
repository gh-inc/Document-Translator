"""Worker-aware readiness reports only persistently stale inflight chunks."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.adapters.persistence.database import SqliteConnectionFactory, transaction
from app.api.main import create_app
from app.config import Settings


@pytest.fixture
async def health_runtime(tmp_path: Path) -> AsyncIterator[tuple[Settings, httpx.AsyncClient]]:
    settings = Settings(
        database_path=tmp_path / "health.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "out",
        llm_provider="fake",
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield settings, client


async def _store_chunk(
    settings: Settings,
    *,
    status: str,
    lease_expires_at: datetime | None,
) -> None:
    connection = await SqliteConnectionFactory(settings.database_path).create()
    try:
        async with transaction(connection):
            await connection.execute(
                "INSERT INTO documents (id, filename, format, size_bytes, storage_path, status) "
                "VALUES ('doc', 'source.pdf', 'pdf', 1, '/tmp/source.pdf', 'extracted')"
            )
            await connection.execute(
                "INSERT INTO jobs (id, document_id, batch_id, target_language, status, model, "
                "prompt_version, idempotency_key) "
                "VALUES ('job', 'doc', 'batch', 'de', 'running', 'fake', 'v1', 'request')"
            )
            await connection.execute(
                "INSERT INTO chunks (id, job_id, seq, status, lease_expires_at) "
                "VALUES ('chunk', 'job', 0, ?, ?)",
                (
                    status,
                    None
                    if lease_expires_at is None
                    else lease_expires_at.astimezone(UTC).isoformat(timespec="microseconds"),
                ),
            )
    finally:
        await connection.close()


async def test_readiness_is_healthy_without_inflight_chunks(
    health_runtime: tuple[Settings, httpx.AsyncClient],
) -> None:
    _, client = health_runtime

    assert (await client.get("/healthz")).status_code == 200
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_rejects_stale_inflight_chunk_with_safe_envelope(
    health_runtime: tuple[Settings, httpx.AsyncClient],
) -> None:
    settings, client = health_runtime
    await _store_chunk(
        settings,
        status="inflight",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=180),
    )

    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "error_code": "not_ready",
        "message": "Service dependencies are unavailable",
        "retryable": True,
    }
    # Liveness remains independent of worker and dependency readiness.
    assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_readiness_accepts_recovered_pending_chunk(
    health_runtime: tuple[Settings, httpx.AsyncClient],
) -> None:
    settings, client = health_runtime
    await _store_chunk(settings, status="pending", lease_expires_at=None)

    response = await client.get("/readyz")

    assert response.status_code == 200


@pytest.mark.parametrize(
    "expiry_offset_seconds",
    [30, -60],
    ids=["fresh-lease", "expired-within-grace"],
)
async def test_readiness_accepts_fresh_and_within_grace_leases(
    health_runtime: tuple[Settings, httpx.AsyncClient], expiry_offset_seconds: int
) -> None:
    settings, client = health_runtime
    lease_expiry = datetime.now(UTC) + timedelta(seconds=expiry_offset_seconds)
    await _store_chunk(settings, status="inflight", lease_expires_at=lease_expiry)

    response = await client.get("/readyz")

    assert response.status_code == 200


async def test_readiness_grace_scales_with_custom_chunk_lease(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_path=tmp_path / "long-lease.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "out",
        llm_provider="fake",
        chunk_lease_seconds=90,
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        # The configured 90-second lease implies a 180-second readiness grace.
        await _store_chunk(
            settings,
            status="inflight",
            lease_expires_at=datetime.now(UTC) - timedelta(seconds=150),
        )
        assert (await client.get("/readyz")).status_code == 200

        # Once the same lease is older than that derived grace, readiness drops.
        connection = await SqliteConnectionFactory(settings.database_path).create()
        try:
            async with transaction(connection):
                await connection.execute(
                    "UPDATE chunks SET lease_expires_at = ? WHERE id = 'chunk'",
                    (
                        (datetime.now(UTC) - timedelta(seconds=181)).isoformat(
                            timespec="microseconds"
                        ),
                    ),
                )
        finally:
            await connection.close()
        assert (await client.get("/readyz")).status_code == 503
