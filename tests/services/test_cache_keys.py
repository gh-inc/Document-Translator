"""Cache identity covers every semantic input to translation."""

import pytest

from app.core.models import TranslationPlan, TriageStatus
from app.core.services.cache_keys import translation_key


def _plan(**changes: object) -> TranslationPlan:
    plan = TranslationPlan(
        source_language="en",
        domain="general",
        register="neutral",
        terms=["invoice"],
        warnings=[],
    )
    return plan.model_copy(update=changes)


def test_key_is_stable_under_glossary_reordering() -> None:
    plan = _plan()
    assert translation_key("de", "m", "v1", {"B": "b", "A": "a"}, plan) == translation_key(
        "de", "m", "v1", {"A": "a", "B": "b"}, plan
    )


@pytest.mark.parametrize(
    "change",
    [
        {"target_language": "uk"},
        {"model": "other"},
        {"prompt_version": "v2"},
        {"glossary": {"A": "changed"}},
    ],
)
def test_key_changes_with_non_plan_input(change: dict[str, object]) -> None:
    plan = _plan()
    arguments = dict(target_language="de", model="m", prompt_version="v1", glossary={}, plan=plan)
    arguments.update(change)
    assert translation_key(**arguments) != translation_key("de", "m", "v1", {}, plan)


@pytest.mark.parametrize(
    "change",
    [
        {"source_language": "de"},
        {"domain": "legal"},
        {"register": "formal"},
        {"terms": ["invoice", "due"]},
        {"warnings": ["ambiguous"]},
        {"triage_status": TriageStatus.DEGRADED},
    ],
)
def test_key_changes_with_plan_field(change: dict[str, object]) -> None:
    assert translation_key("de", "m", "v1", {}, _plan()) != translation_key(
        "de", "m", "v1", {}, _plan(**change)
    )


def test_plan_list_order_is_significant() -> None:
    assert translation_key("de", "m", "v1", {}, _plan(terms=["a", "b"])) != translation_key(
        "de", "m", "v1", {}, _plan(terms=["b", "a"])
    )
