"""Environment-backed application settings."""

from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Paths used by persistence and artifact storage."""

    database_path: Path = Path("/data/app.db")
    upload_storage_path: Path = Path("/data/uploads")
    output_storage_path: Path = Path("/data/out")
