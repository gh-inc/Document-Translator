"""Tests for the model pricing adapter."""

import pytest

from app.adapters.llm.pricing import ModelCostCalculator
from app.core.ports import CostCalculator


def _estimate_through_port(
    calculator: CostCalculator, model: str, tokens_in: int, tokens_out: int
) -> float:
    return calculator.estimate(model, tokens_in, tokens_out)


def test_model_cost_calculator_conforms_to_cost_calculator_port() -> None:
    calculator: CostCalculator = ModelCostCalculator()

    assert _estimate_through_port(calculator, "gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(
        0.75
    )


@pytest.mark.parametrize(
    ("model", "tokens_in", "tokens_out", "expected"),
    [
        ("gpt-4o-mini", 0, 0, 0.0),
        ("gpt-4o-mini", 200_000, 100_000, 0.09),
        ("gpt-4o", 1_000_000, 0, 2.5),
        ("gpt-4o", 0, 1_000_000, 10.0),
    ],
)
def test_estimate_uses_asymmetric_input_and_output_prices(
    model: str, tokens_in: int, tokens_out: int, expected: float
) -> None:
    assert ModelCostCalculator().estimate(model, tokens_in, tokens_out) == pytest.approx(expected)


def test_estimate_rejects_unknown_model() -> None:
    with pytest.raises(ValueError, match="Unknown model"):
        ModelCostCalculator().estimate("unknown-model", 1, 1)


@pytest.mark.parametrize(
    ("tokens_in", "tokens_out"),
    [(-1, 0), (0, -1)],
)
def test_estimate_rejects_negative_token_counts(tokens_in: int, tokens_out: int) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ModelCostCalculator().estimate("gpt-4o-mini", tokens_in, tokens_out)
