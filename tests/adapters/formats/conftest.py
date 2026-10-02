"""Keep FakeProvider format integration tests offline."""

import pytest
from tiktoken import Encoding

from app.adapters.llm import fake_provider

_OFFLINE_ENCODING = Encoding(
    name="format-integration-test-encoding",
    pat_str=r"(?s).",
    mergeable_ranks={bytes([value]): value for value in range(256)},
    special_tokens={},
)


@pytest.fixture(autouse=True)
def offline_tiktoken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fake_provider, "_get_encoder", lambda: _OFFLINE_ENCODING)
