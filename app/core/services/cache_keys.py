"""Stable semantic cache identity, independent of job, document and block IDs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from app.core.models import TranslationPlan


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def plan_hash(plan: TranslationPlan) -> str:
    """Hash all fields rendered into the provider prompt, preserving list order."""
    return _digest(_canonical(plan.model_dump(mode="json")))


def translation_key(
    target_language: str,
    model: str,
    prompt_version: str,
    glossary: Mapping[str, str],
    plan: TranslationPlan,
) -> str:
    """Identify translation configuration; source text enters via lookup hash."""
    return _digest(
        _canonical(
            [
                target_language,
                model,
                prompt_version,
                _digest(_canonical(dict(glossary))),
                plan_hash(plan),
            ]
        )
    )
