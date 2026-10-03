"""Environment-backed application settings."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application and worker settings."""

    # Deployment env files often include variables for Compose or adjacent
    # services. Ignore those unrelated entries while validating this app's
    # own settings.
    model_config = SettingsConfigDict(extra="ignore")

    database_path: Path = Path("/data/app.db")
    upload_storage_path: Path = Path("/data/uploads")
    output_storage_path: Path = Path("/data/out")
    mcp_shared_dir: Path = Path("/mcp-files")
    mcp_triage_poll_interval_seconds: float = Field(
        default=0.1, gt=0.0, le=5.0, allow_inf_nan=False
    )
    mcp_triage_timeout_seconds: float = Field(default=45.0, gt=0.0, le=45.0, allow_inf_nan=False)
    triage_max_turns: int = Field(
        default=16,
        ge=1,
        le=20,
        description="Maximum triage agent turns; serial tool calls make this the tool-call cap.",
    )
    triage_timeout_seconds: float = Field(
        default=60.0,
        gt=0.0,
        le=300.0,
        allow_inf_nan=False,
        description=(
            "Triage agent timeout. Increasing it does not extend MCP polling, which has its own "
            "client timeout while background triage continues."
        ),
    )
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = Field(default="gpt-4o-mini", min_length=1)
    triage_model: str = Field(default="gpt-4o", min_length=1)
    llm_provider: Literal["openai", "fake"] = "openai"
    fake_fail_rate: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    fake_latency_ms: int = Field(default=0, ge=0)
    fake_fail_mode: Literal["429", "500", "timeout"] = "timeout"
    worker_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    job_lease_seconds: int = Field(default=60, gt=0)
    chunk_lease_seconds: int = Field(default=60, gt=0)
    heartbeat_interval_seconds: int = Field(default=10, gt=0)
    max_chunk_concurrency: int = Field(default=8, gt=0)
    max_chunk_attempts: int = Field(default=4, gt=0)
    max_cost_per_job_usd: float = Field(default=2.0, gt=0.0, allow_inf_nan=False)

    @field_validator("worker_id")
    @classmethod
    def worker_id_is_nonempty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("worker_id must not be empty")
        return normalized

    @model_validator(mode="after")
    def heartbeat_fits_leases(self) -> "Settings":
        if self.heartbeat_interval_seconds >= self.job_lease_seconds:
            raise ValueError("heartbeat_interval_seconds must be less than job_lease_seconds")
        if self.heartbeat_interval_seconds >= self.chunk_lease_seconds:
            raise ValueError("heartbeat_interval_seconds must be less than chunk_lease_seconds")
        return self
