"""Run the shared LLM port contract without network calls."""

from app.adapters.llm.fake_provider import FakeProvider
from tests.adapters.llm.provider_contract import run_provider_smoke


async def test_fake_provider_obeys_port_contract() -> None:
    await run_provider_smoke(FakeProvider(fail_rate=0, latency_ms=0))
