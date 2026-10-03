"""Tests for the model pricing adapter."""

import pytest

from app.adapters.llm.pricing import ModelCostCalculator
from app.core.ports import CostCalculator


def _estimate_through_port(
    calculator: CostCalculator, model: str, tokens_in: int, tokens_out: int
) -> float:
    return calculator.estimate(model, tokens_in, tokens_out)


def _estimate_usage_through_port(
    calculator: CostCalculator,
    model: str,
    tokens_in: int,
    tokens_out: int,
    cached_tokens_in: int,
) -> float:
    return calculator.estimate_usage(model, tokens_in, tokens_out, cached_tokens_in)


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


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4o"])
@pytest.mark.parametrize("tokens_in,tokens_out", [(0, 0), (200_000, 100_000), (10, 20)])
def test_estimate_usage_without_cached_tokens_matches_estimate(
    model: str, tokens_in: int, tokens_out: int
) -> None:
    calculator = ModelCostCalculator()

    assert calculator.estimate_usage(model, tokens_in, tokens_out) == calculator.estimate(
        model, tokens_in, tokens_out
    )


def test_estimate_usage_prices_cached_input_at_its_lower_rate() -> None:
    calculator: CostCalculator = ModelCostCalculator()

    uncached = _estimate_usage_through_port(calculator, "gpt-4o-mini", 100_000, 0, 0)
    cached = _estimate_usage_through_port(calculator, "gpt-4o-mini", 100_000, 0, 100_000)

    assert uncached == pytest.approx(0.015)
    assert cached == pytest.approx(0.0075)
    assert cached < uncached


@pytest.mark.parametrize("cached_tokens_in", [-1, 11])
def test_estimate_usage_rejects_cached_tokens_outside_input_range(
    cached_tokens_in: int,
) -> None:
    with pytest.raises(ValueError, match="cached_tokens_in"):
        ModelCostCalculator().estimate_usage("gpt-4o-mini", 10, 0, cached_tokens_in)


def test_estimate_usage_rejects_unknown_model() -> None:
    with pytest.raises(ValueError, match="Unknown model"):
        ModelCostCalculator().estimate_usage("unknown-model", 1, 1, 0)


@pytest.mark.parametrize(
    "model,input_rate,cached_rate,output_rate",
    [("gpt-6-luna", 0.10, 0.01, 0.50), ("gpt-5.6-luna", 0.20, 0.02, 1.20)],
)
def test_luna_reported_usage_snapshot(model, input_rate, cached_rate, output_rate):
    calculator = ModelCostCalculator()
    assert calculator.estimate_usage(model, 100_000, 100_000, 50_000) == pytest.approx(
        (input_rate / 2 + cached_rate / 2 + output_rate) / 10
    )
    assert calculator.estimate(model, 100_000, 100_000) == pytest.approx(
        (input_rate + output_rate) / 10
    )
