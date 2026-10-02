from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


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
        "FAKE_FAIL_RATE",
        "FAKE_LATENCY_MS",
        "FAKE_FAIL_MODE",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings()

    assert settings.openai_api_key == SecretStr("")
    assert settings.openai_model == "gpt-4o-mini"
    assert settings.fake_fail_rate == 0.0
    assert settings.fake_latency_ms == 0
    assert settings.fake_fail_mode == "timeout"

    settings = Settings(openai_api_key="sk-secret-test-value")
    assert "sk-secret-test-value" not in repr(settings)
    assert "**********" in repr(settings)


def test_provider_settings_read_environment_and_validate_values(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-test-value")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("FAKE_FAIL_RATE", "0.25")
    monkeypatch.setenv("FAKE_LATENCY_MS", "10")
    monkeypatch.setenv("FAKE_FAIL_MODE", "500")

    settings = Settings()

    assert settings.openai_api_key.get_secret_value() == "sk-env-test-value"
    assert settings.openai_model == "gpt-4o"
    assert settings.fake_fail_rate == 0.25
    assert settings.fake_latency_ms == 10
    assert settings.fake_fail_mode == "500"

    with pytest.raises(ValidationError):
        Settings(fake_fail_rate=1.1)
    with pytest.raises(ValidationError):
        Settings(fake_fail_rate=float("nan"))
    with pytest.raises(ValidationError):
        Settings(fake_latency_ms=-1)
    with pytest.raises(ValidationError):
        Settings(fake_fail_mode="400")
