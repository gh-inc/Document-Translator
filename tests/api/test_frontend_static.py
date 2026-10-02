"""Same-origin frontend serving preserves API errors and build containment."""

import json
from collections.abc import AsyncIterator
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from app.api.main import create_app
from app.config import Settings

INDEX = "<!doctype html><html><body>Translator SPA</body></html>"
NOT_FOUND = {
    "error_code": "not_found",
    "message": "Requested resource was not found",
    "retryable": False,
}


@pytest.fixture
def frontend_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "dist"
    directory.mkdir()
    (directory / "index.html").write_text(INDEX)
    (directory / "assets").mkdir()
    (directory / "assets" / "app.js").write_text("console.log('translator')")
    (directory / "logo.svg").write_text("<svg></svg>")
    (directory / "branding").mkdir()
    (directory / "branding" / "starkfuture-wordmark.svg").write_text("<svg>Stark</svg>")
    # A frontend build can never shadow the API namespace.
    (directory / "api").mkdir()
    (directory / "api" / "nonexistent").write_text("frontend API impostor")
    return directory


@pytest.fixture
async def client(frontend_dir: Path, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        database_path=tmp_path / "app.db",
        upload_storage_path=tmp_path / "uploads",
        output_storage_path=tmp_path / "out",
        llm_provider="fake",
    )
    settings.upload_storage_path.mkdir()
    settings.output_storage_path.mkdir()
    app = create_app(settings, frontend_dir=frontend_dir)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as runtime,
    ):
        yield runtime


@pytest.mark.parametrize("path", ["/", "/history", "/jobs/job-123", "/history?status=done"])
async def test_spa_navigation_serves_index(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.text == INDEX


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("route", ["batches", "jobs"])
async def test_dotted_identifier_deep_links_serve_spa(
    client: httpx.AsyncClient, method: str, route: str
) -> None:
    # JobService.create_jobs builds batch IDs from the request-key digest and
    # sorted-language JSON digest, joined by a dot.
    request_digest = sha256(b"request-key").hexdigest()
    language_digest = sha256(json.dumps(["de", "fr"], separators=(",", ":")).encode()).hexdigest()
    identifier = f"{request_digest}.{language_digest}"
    response = await client.request(method, f"/{route}/{identifier}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert int(response.headers["content-length"]) == len(INDEX)
    assert response.text == (INDEX if method == "GET" else "")


@pytest.mark.parametrize("path", ["/batches/id/missing.js", "/jobs/id/missing.js", "/other/id.js"])
async def test_dotted_route_exception_requires_exact_spa_shape(
    client: httpx.AsyncClient, path: str
) -> None:
    response = await client.get(path)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize("path", ["/api", "/api/", "/api/nonexistent"])
async def test_api_misses_remain_catalogued_json(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "path", ["/assets/missing.js", "/assets/missing", "/assets", "/missing.svg", "/nested/app.css"]
)
async def test_missing_assets_remain_json(client: httpx.AsyncClient, path: str) -> None:
    response = await client.get(path)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "path", ["/branding", "/branding/", "/branding/missing", "/branding/missing.svg"]
)
async def test_branding_misses_remain_catalogued_json(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    if method == "GET":
        assert response.json() == NOT_FOUND
    else:
        assert response.content == b""


@pytest.mark.parametrize(
    ("path", "content", "media_type"),
    [
        ("/assets/app.js", "console.log('translator')", "text/javascript"),
        ("/logo.svg", "<svg></svg>", "image/svg+xml"),
        ("/branding/starkfuture-wordmark.svg", "<svg>Stark</svg>", "image/svg+xml"),
    ],
)
async def test_existing_assets_are_served(
    client: httpx.AsyncClient, path: str, content: str, media_type: str
) -> None:
    response = await client.get(path)
    assert response.status_code == 200
    assert response.text == content
    assert response.headers["content-type"].startswith(media_type)


@pytest.mark.parametrize(
    "path", ["/", "/history", "/assets/app.js", "/branding/starkfuture-wordmark.svg"]
)
async def test_head_serves_headers_without_body(client: httpx.AsyncClient, path: str) -> None:
    response = await client.head(path)
    assert response.status_code == 200
    assert int(response.headers["content-length"]) > 0
    assert response.content == b""


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def test_spa_fallback_only_handles_navigation_methods(
    client: httpx.AsyncClient, method: str
) -> None:
    response = await client.request(method, "/history")
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_existing_routes_and_errors_are_preserved(client: httpx.AsyncClient) -> None:
    health = await client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    readiness = await client.get("/readyz")
    assert readiness.status_code == 200
    assert readiness.json() == {"status": "ok"}
    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert "jobs_by_status" in metrics.text
    jobs = await client.get("/api/jobs")
    assert jobs.status_code == 200
    assert jobs.json() == []
    missing_document = await client.get("/api/documents/missing")
    assert missing_document.status_code == 404
    assert missing_document.json() == NOT_FOUND
    wrong_method = await client.post("/healthz")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error_code"] == "invalid_request"


@pytest.mark.parametrize("path", ["/", "/history", "/assets/app.js"])
async def test_missing_build_returns_json(
    client: httpx.AsyncClient, frontend_dir: Path, path: str
) -> None:
    (frontend_dir / "index.html").unlink()
    response = await client.get(path)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize(
    "path", ["/%2e%2e/secret.txt", "/assets/%2e%2e/%2e%2e/secret.txt", "/%5csecret.txt"]
)
async def test_path_escape_is_rejected(
    client: httpx.AsyncClient, frontend_dir: Path, path: str
) -> None:
    (frontend_dir.parent / "secret.txt").write_text("private")
    response = await client.get(path)
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


@pytest.mark.parametrize("name", ["escaped.txt", "escaped-route"])
async def test_symlink_escape_never_serves_file_or_spa(
    client: httpx.AsyncClient, frontend_dir: Path, name: str
) -> None:
    secret = frontend_dir.parent / "secret.txt"
    secret.write_text("private")
    (frontend_dir / name).symlink_to(secret)
    response = await client.get(f"/{name}")
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_index_symlink_escape_disables_build(
    client: httpx.AsyncClient, frontend_dir: Path
) -> None:
    secret = frontend_dir.parent / "secret.html"
    secret.write_text("private")
    (frontend_dir / "index.html").unlink()
    (frontend_dir / "index.html").symlink_to(secret)
    response = await client.get("/history")
    assert response.status_code == 404
    assert response.json() == NOT_FOUND


async def test_absent_dist_directory_returns_normal_not_found(tmp_path: Path) -> None:
    app = create_app(frontend_dir=tmp_path / "not-built")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in ("/", "/history", "/assets/app.js"):
            response = await client.get(path)
            assert response.status_code == 404
            assert response.json() == NOT_FOUND
