"""Use an in-memory tiktoken encoding so provider tests never fetch model data."""

from __future__ import annotations

import pytest
from tiktoken import Encoding

from app.adapters.llm import fake_provider

_OFFLINE_ENCODING = Encoding(
    name="fake-provider-test-encoding",
    pat_str=r"(?s).",
    mergeable_ranks={bytes([value]): value for value in range(256)},
    special_tokens={},
)


@pytest.fixture(autouse=True)
def offline_tiktoken(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent normal provider tests from calling tiktoken's network loader."""
    monkeypatch.setattr(fake_provider, "_get_encoder", lambda: _OFFLINE_ENCODING)
