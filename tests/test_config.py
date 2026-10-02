from pathlib import Path

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
