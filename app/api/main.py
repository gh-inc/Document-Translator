"""FastAPI application factory and startup initialization."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.adapters.persistence.database import SqliteConnectionFactory
from app.api.background import create_triage_agent
from app.api.errors import register_exception_handlers
from app.api.frontend import register_frontend_handler
from app.api.routers import documents, health, jobs
from app.config import Settings


def create_app(settings: Settings | None = None, *, frontend_dir: Path | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        # WAL and schema must be initialized before accepting requests.
        connection = await SqliteConnectionFactory(
            application.state.settings.database_path
        ).create()
        await connection.close()
        yield

    application = FastAPI(title="Document Translator", lifespan=lifespan)
    application.state.settings = settings if settings is not None else Settings()
    application.state.upload_lock = asyncio.Lock()
    application.state.triage_agent_factory = create_triage_agent
    register_exception_handlers(application)
    application.include_router(documents.router)
    application.include_router(jobs.router)
    application.include_router(health.router)
    register_frontend_handler(
        application,
        frontend_dir
        if frontend_dir is not None
        else Path(__file__).parents[2] / "frontend" / "dist",
    )
    return application


app = create_app()
