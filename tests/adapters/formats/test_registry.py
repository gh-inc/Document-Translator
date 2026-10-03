"""Tests for bounded, signature-checked format resolution."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from app.adapters.formats.registry import FormatRegistry
from app.core.models import Block


def _registry() -> tuple[FormatRegistry, object, object]:
    registry = FormatRegistry()
    pdf_extractor = object()
    pdf_renderer = object()
    docx_extractor = object()
    docx_renderer = object()
    markdown_extractor = object()
    markdown_renderer = object()
    registry.register(".PDF", pdf_extractor, pdf_renderer)  # type: ignore[arg-type]
    registry.register("docx", docx_extractor, docx_renderer)  # type: ignore[arg-type]
    registry.register("md", markdown_extractor, markdown_renderer)  # type: ignore[arg-type]
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


async def test_resolve_accepts_utf8_markdown_and_boundary_codepoint(
    tmp_path: Path,
) -> None:
    registry, _, _ = _registry()
    file_path = tmp_path / "document.md"
    # The first byte of "é" is the final byte inspected by detection. The
    # complete file is valid UTF-8, so the partial header code point is allowed.
    file_path.write_bytes(b"a" * 2047 + "é".encode() + b"\n")

    assert await registry.resolve(file_path) == registry._adapters["md"]


@pytest.mark.parametrize("content", [b"\xffinvalid", b"valid\x00text", b"# truncated \xe2"])
async def test_resolve_rejects_invalid_utf8_nul_and_truncated_eof(
    tmp_path: Path, content: bytes
) -> None:
    registry, _, _ = _registry()
    file_path = tmp_path / "document.md"
    file_path.write_bytes(content)

    assert await registry.resolve(file_path) is None


def test_skip_block_ids_dispatches_only_to_registered_markdown_adapter() -> None:
    class Extractor:
        def get_skip_block_ids(self, blocks: list[Block]) -> set[str]:
            assert [block.id for block in blocks] == ["one"]
            return {"one"}

    registry = FormatRegistry()
    registry.register("md", Extractor(), object())  # type: ignore[arg-type]
    blocks = [Block(id="one", seq=0, source_text="", source_hash="empty")]

    assert registry.get_skip_block_ids(".MD", blocks) == {"one"}
    assert registry.get_skip_block_ids("pdf", blocks) == set()
    assert registry.get_skip_block_ids("docx", blocks) == set()
    assert FormatRegistry().get_skip_block_ids("md", blocks) == set()


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

        def fileno(self) -> int:
            return self._reader.fileno()

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
