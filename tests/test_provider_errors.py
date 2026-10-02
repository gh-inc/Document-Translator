"""Safe errors preserve retry decisions and known billed token usage."""

import pytest

from app.core.errors import DocumentError, ErrorCode, ProviderError


@pytest.mark.parametrize("code", [code for code in ErrorCode if code.value.startswith("provider_")])
def test_catalogued_provider_error_has_safe_message_and_retryability(code: ErrorCode) -> None:
    error = ProviderError(code, tokens_in=12, tokens_out=7, model="gpt-4o-mini")
    assert error.error_code == code
    assert str(error) == error.message
    assert error.message
    assert error.retryable is (
        code
        not in {
            ErrorCode.PROVIDER_BAD_REQUEST,
            ErrorCode.PROVIDER_AUTH_ERROR,
            ErrorCode.PROVIDER_REFUSAL,
        }
    )
    assert error.tokens_in == 12
    assert error.tokens_out == 7
    assert error.model == "gpt-4o-mini"


@pytest.mark.parametrize(
    "code", [ErrorCode.SCANNED_PDF, ErrorCode.CORRUPT_FILE, ErrorCode.RENDER_FAILED]
)
def test_document_errors_use_safe_non_retryable_catalog(code: ErrorCode) -> None:
    error = DocumentError(code)
    assert error.error_code == code
    assert str(error) == error.message
    assert error.message
    assert error.retryable is False


def test_cost_cap_error_is_catalogued_and_fatal() -> None:
    error = ProviderError(ErrorCode.COST_CAP_EXCEEDED)
    assert error.error_code == "cost_cap_exceeded"
    assert error.message == "Job translation cost limit reached"
    assert error.retryable is False
