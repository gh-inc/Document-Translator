"""Snapshot token pricing for the supported OpenAI models.

The values below are the Stage 2 plan's pricing snapshot, expressed in USD per
million tokens. They are implementation inputs, not a claim about current
provider pricing.
"""

from app.core.ports import CostCalculator

_PRICES_USD_PER_MILLION_TOKENS: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.150, "output": 0.600},
    "gpt-4o": {"input": 2.500, "output": 10.000},
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
