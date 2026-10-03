from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from app.adapters.llm.triage_agent import OpenAITriageAgent
from app.adapters.llm.triage_runtime import create_triage_agent
from app.config import Settings


def test_mcp_settings_defaults_environment_and_bounds(monkeypatch) -> None:
    for name in (
        "MCP_SHARED_DIR",
        "MCP_TRIAGE_POLL_INTERVAL_SECONDS",
        "MCP_TRIAGE_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.mcp_shared_dir == Path("/mcp-files")
    assert settings.mcp_triage_timeout_seconds == 45
    assert settings.mcp_triage_poll_interval_seconds == 0.1
    monkeypatch.setenv("MCP_SHARED_DIR", "/tmp/shared")
    monkeypatch.setenv("MCP_TRIAGE_TIMEOUT_SECONDS", "10")
    assert Settings().mcp_shared_dir == Path("/tmp/shared")
    assert Settings().mcp_triage_timeout_seconds == 10
    for value in (0, -1, 45.1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            Settings(mcp_triage_timeout_seconds=value)
    for value in (0, -1, 5.1, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            Settings(mcp_triage_poll_interval_seconds=value)


def test_triage_limits_have_defaults_environment_values_and_bounds(monkeypatch) -> None:
    for name in ("TRIAGE_MAX_TURNS", "TRIAGE_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()
    assert settings.triage_max_turns == 16
    assert settings.triage_timeout_seconds == 60.0

    monkeypatch.setenv("TRIAGE_MAX_TURNS", "3")
    monkeypatch.setenv("TRIAGE_TIMEOUT_SECONDS", "12")
    settings = Settings()
    assert settings.triage_max_turns == 3
    assert settings.triage_timeout_seconds == 12.0

    for kwargs in (
        {"triage_max_turns": 0},
        {"triage_max_turns": 999},
        {"triage_timeout_seconds": 0.0},
        {"triage_timeout_seconds": float("inf")},
        {"triage_timeout_seconds": 300.1},
    ):
        with pytest.raises(ValidationError):
            Settings(**kwargs)


def test_triage_agent_factory_applies_configured_limits() -> None:
    agent = create_triage_agent(Settings(triage_max_turns=3, triage_timeout_seconds=12.0))

    assert isinstance(agent, OpenAITriageAgent)
    assert agent._max_turns == 3
    assert agent._timeout_seconds == 12.0


def test_settings_have_path_defaults(monkeypatch) -> None:
    for name in ("DATABASE_PATH", "UPLOAD_STORAGE_PATH", "OUTPUT_STORAGE_PATH"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.database_path == Path("/data/app.db")
    assert settings.upload_storage_path == Path("/data/uploads")
    assert settings.output_storage_path == Path("/data/out")


def test_settings_read_paths_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", "/tmp/translator.sqlite")
    monkeypatch.setenv("UPLOAD_STORAGE_PATH", "/tmp/translator/uploads")
    monkeypatch.setenv("OUTPUT_STORAGE_PATH", "/tmp/translator/out")

    settings = Settings()

    assert settings.database_path == Path("/tmp/translator.sqlite")
    assert settings.upload_storage_path == Path("/tmp/translator/uploads")
    assert settings.output_storage_path == Path("/tmp/translator/out")


def test_provider_settings_defaults_and_secret_repr(monkeypatch) -> None:
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "TRIAGE_MODEL",
        "FAKE_FAIL_RATE",
        "FAKE_LATENCY_MS",
        "FAKE_FAIL_MODE",
        "LLM_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.openai_api_key == SecretStr("")
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.triage_model == "gpt-4o"
    assert settings.fake_fail_rate == 0.0
    assert settings.fake_latency_ms == 0
    assert settings.fake_fail_mode == "timeout"
    assert settings.llm_provider == "openai"

    settings = Settings(openai_api_key="sk-secret-test-value")
    assert "sk-secret-test-value" not in repr(settings)
    assert "**********" in repr(settings)


def test_provider_settings_read_environment_and_validate_values(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test-value")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("TRIAGE_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("FAKE_FAIL_RATE", "0.25")
    monkeypatch.setenv("FAKE_LATENCY_MS", "10")
    monkeypatch.setenv("FAKE_FAIL_MODE", "500")
    monkeypatch.setenv("LLM_PROVIDER", "fake")

    settings = Settings()

    assert settings.openai_api_key.get_secret_value() == "sk-env-test-value"
    assert settings.openai_model == "gpt-4o"
    assert settings.triage_model == "gpt-4o-mini"
    assert settings.fake_fail_rate == 0.25
    assert settings.fake_latency_ms == 10
    assert settings.fake_fail_mode == "500"
    assert settings.llm_provider == "fake"

    with pytest.raises(ValidationError):
        Settings(fake_fail_rate=1.1)
    with pytest.raises(ValidationError):
        Settings(fake_fail_rate=float("nan"))
    with pytest.raises(ValidationError):
        Settings(fake_latency_ms=-1)
    with pytest.raises(ValidationError):
        Settings(fake_fail_mode="400")


def test_triage_and_bulk_model_settings_are_independent(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("TRIAGE_MODEL", raising=False)
    assert (Settings().openai_model, Settings().triage_model) == ("gpt-4o-mini", "gpt-4o")

    monkeypatch.setenv("OPENAI_MODEL", "bulk-only")
    assert (Settings().openai_model, Settings().triage_model) == ("bulk-only", "gpt-4o")

    monkeypatch.delenv("OPENAI_MODEL")
    monkeypatch.setenv("TRIAGE_MODEL", "triage-only")
    assert (Settings().openai_model, Settings().triage_model) == ("gpt-4o-mini", "triage-only")

    with pytest.raises(ValidationError):
        Settings(triage_model="")


def test_settings_dotenv_ignores_unrelated_deployment_variables(tmp_path: Path) -> None:
    env_file = tmp_path / "measurement.env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-dotenv-test-value\n"
        "LLM_PROVIDER=openai\n"
        "COMPOSE_PROJECT_NAME=document-translator\n"
        "LEGACY_SERVICE_OPTION=preserved-outside-app-settings\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.openai_api_key.get_secret_value() == "sk-dotenv-test-value"
    assert settings.llm_provider == "openai"
    assert "sk-dotenv-test-value" not in repr(settings)


def test_worker_settings_defaults_and_validate_constraints(monkeypatch) -> None:
    for name in (
        "WORKER_ID",
        "JOB_LEASE_SECONDS",
        "CHUNK_LEASE_SECONDS",
        "HEARTBEAT_INTERVAL_SECONDS",
        "MAX_CHUNK_CONCURRENCY",
        "MAX_CHUNK_ATTEMPTS",
        "MAX_COST_PER_JOB_USD",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert UUID(settings.worker_id)
    assert settings.job_lease_seconds == 60
    assert settings.chunk_lease_seconds == 60
    assert settings.heartbeat_interval_seconds == 10
    assert settings.max_chunk_concurrency == 8
    assert settings.max_chunk_attempts == 4
    assert settings.max_cost_per_job_usd == 2.0

    with pytest.raises(ValidationError):
        Settings(worker_id=" ")
    with pytest.raises(ValidationError):
        Settings(job_lease_seconds=0)
    with pytest.raises(ValidationError):
        Settings(chunk_lease_seconds=-1)
    with pytest.raises(ValidationError):
        Settings(heartbeat_interval_seconds=0)
    with pytest.raises(ValidationError):
        Settings(max_chunk_concurrency=0)
    with pytest.raises(ValidationError):
        Settings(max_chunk_attempts=-1)
    with pytest.raises(ValidationError):
        Settings(max_cost_per_job_usd=float("inf"))
    with pytest.raises(ValidationError):
        Settings(heartbeat_interval_seconds=60)
    with pytest.raises(ValidationError):
        Settings(chunk_lease_seconds=9)
