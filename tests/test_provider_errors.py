"""Safe errors preserve retry decisions and known billed token usage."""

import pytest

from app.core.errors import ErrorCode, ProviderError


@pytest.mark.parametrize("code", list(ErrorCode))
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
