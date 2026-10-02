"""Environment-backed application settings."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Paths used by persistence and artifact storage."""

    database_path: Path = Path("/data/app.db")
    upload_storage_path: Path = Path("/data/uploads")
    output_storage_path: Path = Path("/data/out")
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-4o-mini"
    fake_fail_rate: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    fake_latency_ms: int = Field(default=0, ge=0)
    fake_fail_mode: Literal["429", "500", "timeout"] = "timeout"
