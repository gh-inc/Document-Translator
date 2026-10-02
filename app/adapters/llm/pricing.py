"""Unwired cost calculator skeleton implementing the CostCalculator port."""

from app.core.ports import CostCalculator


class ModelCostCalculator(CostCalculator):
    """Skeleton cost calculator; model pricing data and calculations are not wired yet."""

    def estimate(self, model: str, tokens_in: int, tokens_out: int) -> float:
        raise NotImplementedError("Cost calculator is not wired yet")
