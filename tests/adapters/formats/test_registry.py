"""Tests for bounded, signature-checked format resolution."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.adapters.formats.registry import FormatRegistry


def _registry() -> tuple[FormatRegistry, object, object]:
    registry = FormatRegistry()
    pdf_extractor = object()
    pdf_renderer = object()
    docx_extractor = object()
    docx_renderer = object()
    registry.register(".PDF", pdf_extractor, pdf_renderer)  # type: ignore[arg-type]
    registry.register("docx", docx_extractor, docx_renderer)  # type: ignore[arg-type]
    return registry, (pdf_extractor, pdf_renderer), (docx_extractor, docx_renderer)


@pytest.mark.parametrize(
    ("filename", "signature", "expected_index"),
    [
        ("document.PDF", b"%PDF-1.7\n", 1),
        ("document.docx", b"PK\x03\x04zip data", 2),
        ("stored-upload", b"%PDF-1.7\n", 1),
        ("stored-upload", b"PK\x03\x04zip data", 2),
    ],
)
async def test_resolve_supported_extension_or_extensionless_signature(
    tmp_path: Path,
    filename: str,
    signature: bytes,
    expected_index: int,
) -> None:
    registry, pdf_adapter, docx_adapter = _registry()
    file_path = tmp_path / filename
    file_path.write_bytes(signature)

    resolved = await registry.resolve(file_path)

    assert resolved == (pdf_adapter, docx_adapter)[expected_index - 1]


@pytest.mark.parametrize(
    ("filename", "signature"),
    [
        ("document.txt", b"%PDF-1.7\n"),
        ("document.pdf", b"PK\x03\x04zip data"),
        ("document.docx", b"%PDF-1.7\n"),
        ("stored-upload", b"not a supported document"),
    ],
)
async def test_resolve_rejects_unsupported_or_mismatched_signature(
    tmp_path: Path,
    filename: str,
    signature: bytes,
) -> None:
    registry, _, _ = _registry()
    file_path = tmp_path / filename
    file_path.write_bytes(signature)

    assert await registry.resolve(file_path) is None


async def test_resolve_returns_none_for_missing_file(tmp_path: Path) -> None:
    registry, _, _ = _registry()

    assert await registry.resolve(tmp_path / "missing.pdf") is None


async def test_resolve_reads_at_most_2048_bytes_off_event_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, pdf_adapter, _ = _registry()
    file_path = tmp_path / "large.pdf"
    file_path.write_bytes(b"%PDF-1.7\n" + b"x" * 8192)
    original_open = Path.open
    read_sizes: list[int] = []
    read_thread_ids: list[int] = []

    class TrackedReader:
        def __init__(self, reader: Any) -> None:
            self._reader = reader

        def __enter__(self) -> TrackedReader:
            self._reader.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self._reader.__exit__(*args)

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            read_thread_ids.append(threading.get_ident())
            return self._reader.read(size)

    def tracked_open(path: Path, *args: object, **kwargs: object) -> object:
        reader = original_open(path, *args, **kwargs)
        if path == file_path:
            return TrackedReader(reader)
        return reader

    monkeypatch.setattr(Path, "open", tracked_open)
    event_loop_thread_id = threading.get_ident()

    assert await registry.resolve(file_path) == pdf_adapter
    assert read_sizes == [2048]
    assert read_thread_ids[0] != event_loop_thread_id
