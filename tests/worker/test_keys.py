"""Semantic cache identity changes only with translation inputs."""

import pytest

from app.worker.keys import translation_key
from tests.adapters.persistence.test_persistence_integration import _aggregate


def test_cache_key_ignores_job_identity_and_glossary_order() -> None:
    job, _, _ = _aggregate()
    job.glossary = {"B": "b", "A": "a"}
    equivalent = job.model_copy(update={"id": "other", "glossary": {"A": "a", "B": "b"}})
    assert translation_key(job) == translation_key(equivalent)


@pytest.mark.parametrize(
    "change",
    [
        {"target_language": "uk"},
        {"model": "other"},
        {"prompt_version": "v2"},
        {"glossary": {"A": "changed"}},
    ],
)
def test_cache_key_includes_every_configured_semantic_input(change: dict[str, object]) -> None:
    job, _, _ = _aggregate()
    assert translation_key(job) != translation_key(job.model_copy(update=change))
