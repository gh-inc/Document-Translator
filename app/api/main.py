"""FastAPI application factory and startup initialization."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters.persistence.database import SqliteConnectionFactory
from app.api.errors import register_exception_handlers
from app.api.routers import documents, health, jobs
from app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
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
    register_exception_handlers(application)
    application.include_router(documents.router)
    application.include_router(jobs.router)
    application.include_router(health.router)
    return application


app = create_app()
