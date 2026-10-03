"""Character similarity and literal-preservation semantics for measurements."""

import pytest

from app.core.quality import _preservation_tokens, chrf_score, preservation_metrics


@pytest.mark.parametrize(
    ("candidate", "reference", "expected"),
    [
        ("abc", "abc", 100.0),
        ("abc", "xyz", 0.0),
        ("", "", 0.0),
        ("", "abc", 0.0),
        ("abc", "", 0.0),
        ("a b\n c", "ab c", 100.0),
        ("abc", "abd", 350 / 9),
    ],
)
def test_chrf_effective_orders_and_whitespace(
    candidate: str, reference: str, expected: float
) -> None:
    assert chrf_score(candidate, reference) == pytest.approx(expected)


def test_chrf_partial_overlap_is_between_disjoint_and_identical() -> None:
    disjoint = chrf_score("abc", "xyz")
    partial = chrf_score("abc", "abd")
    identical = chrf_score("abc", "abc")
    assert disjoint < partial < identical


@pytest.mark.parametrize(
    ("beta", "max_order"),
    [(0, 6), (-1, 6), (2, 0), (2, -1)],
)
def test_chrf_rejects_nonpositive_parameters(beta: int, max_order: int) -> None:
    with pytest.raises(ValueError, match="beta and max_order must be positive"):
        chrf_score("abc", "abc", beta=beta, max_order=max_order)


def test_chrf_beta_weights_recall() -> None:
    assert chrf_score("ab", "abc", beta=2) < chrf_score("ab", "abc", beta=1)


def test_preservation_counts_repeated_literals_by_occurrence() -> None:
    result = preservation_metrics("12, 12, 34", "34, 12")
    assert result["number"] == {
        "source_count": 3,
        "preserved_count": 2,
        "preservation_percent": pytest.approx(200 / 3),
    }
    assert result["source_token_count"] == 3
    assert result["preserved_token_count"] == 2
    assert result["preservation_percent"] == pytest.approx(200 / 3)


def test_preservation_is_order_independent() -> None:
    result = preservation_metrics("12 then 34 then 56", "56 then 12 then 34")
    assert result["number"] == {
        "source_count": 3,
        "preserved_count": 3,
        "preservation_percent": 100.0,
    }
    assert result["preservation_percent"] == 100.0


def test_preservation_without_source_tokens_is_undefined() -> None:
    result = preservation_metrics("Nothing to count", "A new 12 appeared")
    for category in ("placeholder", "date", "currency", "number"):
        assert result[category] == {
            "source_count": 0,
            "preserved_count": 0,
            "preservation_percent": None,
        }
    assert result["source_token_count"] == 0
    assert result["preserved_token_count"] == 0
    assert result["preservation_percent"] is None


def test_preservation_categories_and_literal_matching() -> None:
    source = "{{id}} %s [NAME] 2024-12-31 31/12/2024 $12.50 EUR 80 15% 42"
    translated = "42 15% EUR 80 $12.50 31/12/2024 2024-12-31 [NAME] %s {{id}}"
    assert _preservation_tokens(source) == [
        ("placeholder", "{{id}}"),
        ("placeholder", "%s"),
        ("placeholder", "[NAME]"),
        ("date", "2024-12-31"),
        ("date", "31/12/2024"),
        ("currency", "$12.50"),
        ("currency", "EUR 80"),
        ("number", "15%"),
        ("number", "42"),
    ]
    result = preservation_metrics(source, translated)
    for category, count in (("placeholder", 3), ("date", 2), ("currency", 2), ("number", 2)):
        assert result[category] == {
            "source_count": count,
            "preserved_count": count,
            "preservation_percent": 100.0,
        }
    assert result["source_token_count"] == 9
    assert result["preserved_token_count"] == 9
    assert result["preservation_percent"] == 100.0


def test_preservation_uses_exact_literal_case() -> None:
    result = preservation_metrics("USD 12", "usd 12")
    assert result["currency"] == {
        "source_count": 1,
        "preserved_count": 0,
        "preservation_percent": 0.0,
    }
