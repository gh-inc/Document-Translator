"""Contract tests for core Pydantic models and repository ports."""

from __future__ import annotations

import inspect
from typing import get_type_hints

import pytest
from pydantic import ValidationError

from app.core.models import (
    Block,
    ChunkRecord,
    ChunkRequest,
    DocumentIR,
    JobRecord,
    TranslationPlan,
)
from app.core.ports import (
    DocumentRepository,
    JobExecutionRepository,
    TranslationCacheRepository,
)


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "page": "not-an-integer",
            "bbox": ["left", None, {"unexpected": [1, 2, 3]}],
            "font_size": {"value": "large", "unit": None},
            "future_pdf_feature": [{"nested": {"unknown_key": True}}],
        },
        {
            "paragraph_index": {"not": "an integer"},
            "run_indices": "not-a-list",
            "style": [None, 17, {"future_docx_key": "preserve me"}],
            "future_docx_feature": {"custom": ["data", None, 3.5]},
        },
        {
            "scalar_string": "value",
            "scalar_number": 42,
            "scalar_float": 3.5,
            "scalar_boolean": False,
            "scalar_null": None,
            "list": ["text", 7, False, None, {"deeper": [1, 2]}],
        },
    ],
    ids=("invalid-pdf-shape", "invalid-docx-shape", "json-values"),
)
def test_block_metadata_survives_validation_and_json_roundtrip(
    metadata: dict[str, object],
) -> None:
    block = Block(
        id="block-1",
        seq=1,
        source_text="Hello",
        source_hash="hash",
        format_metadata=metadata,
    )

    dumped = block.model_dump()
    validated = Block.model_validate(dumped)
    json_roundtrip = Block.model_validate_json(block.model_dump_json())

    assert dumped["format_metadata"] == metadata
    assert validated.format_metadata == metadata
    assert json_roundtrip.format_metadata == metadata
    assert json_roundtrip.model_dump()["format_metadata"] == metadata


def test_format_metadata_defaults_to_an_empty_object() -> None:
    block = Block(id="block-1", seq=1, source_text="Hello", source_hash="hash")

    assert block.format_metadata == {}


def test_opaque_metadata_survives_when_block_is_nested_in_core_models() -> None:
    metadata = {"vendor": {"new": [None, {"shape": "unknown"}]}}
    block = Block(
        id="block-1",
        seq=1,
        source_text="Hello",
        source_hash="hash",
        format_metadata=metadata,
    )
    document = DocumentIR(
        id="document-1",
        filename="input.pdf",
        format="pdf",
        size_bytes=10,
        blocks=[block],
    )
    request = ChunkRequest(
        chunk_id="chunk-1",
        blocks=[block],
        target_language="de",
        plan=TranslationPlan(source_language="en", domain="general", register="neutral"),
        model="test-model",
    )

    document_roundtrip = DocumentIR.model_validate_json(document.model_dump_json())
    request_roundtrip = ChunkRequest.model_validate_json(request.model_dump_json())
    assert document_roundtrip.blocks[0].format_metadata == metadata
    assert request_roundtrip.blocks[0].format_metadata == metadata


def test_repository_ports_are_split_by_ownership() -> None:
    expected_methods = {
        DocumentRepository: {"create_document", "create_blocks", "save_analysis"},
        JobExecutionRepository: {
            "create_job_with_chunks",
            "get_job",
            "claim_job",
            "record_chunk_attempt",
        },
        TranslationCacheRepository: {"get_block_translation", "save_block_translation"},
    }

    public_methods: dict[type, set[str]] = {}
    for protocol, methods in expected_methods.items():
        public_methods[protocol] = {
            name
            for name, value in protocol.__dict__.items()
            if not name.startswith("_") and callable(value)
        }
        assert methods <= public_methods[protocol]

    assert not public_methods[DocumentRepository] & public_methods[JobExecutionRepository]
    assert not public_methods[DocumentRepository] & public_methods[TranslationCacheRepository]
    assert not public_methods[JobExecutionRepository] & public_methods[TranslationCacheRepository]


def test_job_creation_port_is_one_aggregate_operation() -> None:
    methods = set(JobExecutionRepository.__dict__)
    assert "create_job_with_chunks" in methods
    assert "create_job" not in methods
    assert "create_chunks" not in methods

    method = JobExecutionRepository.create_job_with_chunks
    signature = inspect.signature(method)
    parameters = list(signature.parameters.values())
    assert [parameter.name for parameter in parameters] == ["self", "job", "chunks"]
    type_hints = get_type_hints(method)
    assert type_hints["job"] is JobRecord
    assert type_hints["chunks"] == list[ChunkRecord]
    assert type_hints["return"] is type(None)
    assert inspect.iscoroutinefunction(method)


def test_block_requires_metadata_to_be_an_object() -> None:
    with pytest.raises(ValidationError):
        Block(
            id="block-1",
            seq=1,
            source_text="Hello",
            source_hash="hash",
            format_metadata=["not", "an", "object"],  # type: ignore[arg-type]
        )
