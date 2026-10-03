"""Pure character-similarity and literal-preservation measurements."""

from __future__ import annotations

import re
from collections import Counter

_PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}|%s|\[[^\[\]]*\]")
_DATE_RE = re.compile(
    r"(?<!\w)(?:\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})(?!\w)"
)
_CURRENCY_CODES = "USD|EUR|GBP|JPY|CHF|CAD|AUD|NZD|CNY|INR|UAH|PLN|SEK|NOK|DKK"
_CURRENCY_RE = re.compile(
    rf"(?<!\w)(?:(?:[$€£¥]\s*|(?:{_CURRENCY_CODES})\s*)[-+]?\d[\d.,]*"
    rf"|[-+]?\d[\d.,]*\s*(?:[$€£¥]|(?:{_CURRENCY_CODES})))(?!\w)",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(
    r"(?<![\w])[-+]?\d+(?:[.,]\d+)*(?:\s?%)(?!\w)|(?<![\w])[-+]?\d+(?:[.,]\d+)*(?!\w)"
)
_TOKEN_RE = re.compile(
    rf"(?P<placeholder>{_PLACEHOLDER_RE.pattern})"
    rf"|(?P<date>{_DATE_RE.pattern})"
    rf"|(?P<currency>{_CURRENCY_RE.pattern})"
    rf"|(?P<number>{_NUMBER_RE.pattern})",
    re.IGNORECASE,
)


def chrf_score(candidate: str, reference: str, *, beta: int = 2, max_order: int = 6) -> float:
    """Return corpus-style chrF on a 0..100 scale, excluding whitespace.

    This implements character n-gram orders 1 through 6 with the standard
    chrF beta=2 weighting. It is intentionally a compact single-reference
    implementation for the measurement command, not a replacement for a
    general-purpose metric library.
    """
    if beta <= 0 or max_order <= 0:
        raise ValueError("beta and max_order must be positive")
    predicted = "".join(candidate.split())
    expected = "".join(reference.split())
    order_precisions: list[float] = []
    order_recalls: list[float] = []
    for order in range(1, max_order + 1):
        predicted_ngrams = Counter(
            predicted[index : index + order] for index in range(max(0, len(predicted) - order + 1))
        )
        expected_ngrams = Counter(
            expected[index : index + order] for index in range(max(0, len(expected) - order + 1))
        )
        predicted_total = sum(predicted_ngrams.values())
        expected_total = sum(expected_ngrams.values())
        if predicted_total and expected_total:
            matched = sum((predicted_ngrams & expected_ngrams).values())
            order_precisions.append(matched / predicted_total)
            order_recalls.append(matched / expected_total)
    if not order_precisions:
        return 0.0
    precision = sum(order_precisions) / len(order_precisions)
    recall = sum(order_recalls) / len(order_recalls)
    beta_squared = beta**2
    denominator = beta_squared * precision + recall
    if denominator == 0:
        return 0.0
    return 100.0 * (1 + beta_squared) * precision * recall / denominator


def preservation_metrics(source: str, translated: str) -> dict[str, object]:
    """Compare source token multisets so repeated literals count separately."""
    source_tokens = _preservation_tokens(source)
    translated_tokens = _preservation_tokens(translated)
    result: dict[str, object] = {}
    total_source = 0
    total_preserved = 0
    for category in ("placeholder", "date", "currency", "number"):
        expected = Counter(token for kind, token in source_tokens if kind == category)
        actual = Counter(token for kind, token in translated_tokens if kind == category)
        count = sum(expected.values())
        preserved = sum(min(amount, actual[token]) for token, amount in expected.items())
        total_source += count
        total_preserved += preserved
        result[category] = {
            "source_count": count,
            "preserved_count": preserved,
            "preservation_percent": 100.0 * preserved / count if count else None,
        }
    result["source_token_count"] = total_source
    result["preserved_token_count"] = total_preserved
    result["preservation_percent"] = (
        100.0 * total_preserved / total_source if total_source else None
    )
    return result


def _preservation_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for match in _TOKEN_RE.finditer(text):
        category = next(name for name, value in match.groupdict().items() if value is not None)
        tokens.append((category, match.group(0)))
    return tokens
