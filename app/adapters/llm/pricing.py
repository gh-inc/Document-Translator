"""Snapshot token pricing for the supported OpenAI models.

Rates are USD per million tokens. Cached-input rates were verified against the
official OpenAI model pages on 2026-10-03; this is a snapshot, not live pricing.
"""

from app.core.ports import CostCalculator

_PRICES_USD_PER_MILLION_TOKENS: dict[str, dict[str, float]] = {
    # OpenAI model pages: https://developers.openai.com/api/docs/models/gpt-4o-mini
    # and https://developers.openai.com/api/docs/models/gpt-4o
    "gpt-4o-mini": {"input": 0.150, "cached_input": 0.075, "output": 0.600},
    "gpt-4o": {"input": 2.500, "cached_input": 1.250, "output": 10.000},
}


class ModelCostCalculator(CostCalculator):
    """Estimate provider cost from token counts using the planned price snapshot."""

    def estimate(self, model: str, tokens_in: int, tokens_out: int) -> float:
        """Return the estimated USD cost for input and output tokens."""
        if tokens_in < 0 or tokens_out < 0:
            raise ValueError("Token counts must be non-negative")

        try:
            prices = _PRICES_USD_PER_MILLION_TOKENS[model]
        except KeyError as exc:
            raise ValueError(f"Unknown model for cost estimation: {model}") from exc

        return (tokens_in * prices["input"] + tokens_out * prices["output"]) / 1_000_000

    def estimate_usage(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cached_tokens_in: int = 0,
    ) -> float:
        """Estimate cost while pricing cached input at the model's cached rate."""
        if tokens_in < 0 or tokens_out < 0:
            raise ValueError("Token counts must be non-negative")
        if cached_tokens_in < 0 or cached_tokens_in > tokens_in:
            raise ValueError("cached_tokens_in must be between zero and tokens_in")

        try:
            prices = _PRICES_USD_PER_MILLION_TOKENS[model]
        except KeyError as exc:
            raise ValueError(f"Unknown model for cost estimation: {model}") from exc

        uncached_tokens_in = tokens_in - cached_tokens_in
        return (
            uncached_tokens_in * prices["input"]
            + cached_tokens_in * prices["cached_input"]
            + tokens_out * prices["output"]
        ) / 1_000_000
