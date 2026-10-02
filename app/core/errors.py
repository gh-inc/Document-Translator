"""Safe catalogued failures shared by adapters and the future worker."""

from enum import StrEnum


class ErrorCode(StrEnum):
    SCANNED_PDF = "scanned_pdf"
    CORRUPT_FILE = "corrupt_file"
    RENDER_FAILED = "render_failed"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_CONNECTION = "provider_connection"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    PROVIDER_BAD_REQUEST = "provider_bad_request"
    PROVIDER_AUTH_ERROR = "provider_auth_error"
    PROVIDER_INVALID_RESPONSE = "provider_invalid_response"
    PROVIDER_REFUSAL = "provider_refusal"


_CATALOG: dict[ErrorCode, tuple[str, bool]] = {
    ErrorCode.SCANNED_PDF: ("PDF has no usable text layer; OCR is unsupported", False),
    ErrorCode.CORRUPT_FILE: ("Document cannot be read or is corrupt", False),
    ErrorCode.RENDER_FAILED: ("Translated document could not be rendered", False),
    ErrorCode.PROVIDER_TIMEOUT: ("Translation provider timed out", True),
    ErrorCode.PROVIDER_CONNECTION: ("Translation provider is unreachable", True),
    ErrorCode.PROVIDER_RATE_LIMIT: ("Translation provider rate limit reached", True),
    ErrorCode.PROVIDER_SERVER_ERROR: ("Translation provider is temporarily unavailable", True),
    ErrorCode.PROVIDER_BAD_REQUEST: ("Translation provider rejected the request", False),
    ErrorCode.PROVIDER_AUTH_ERROR: ("Translation provider authorization failed", False),
    ErrorCode.PROVIDER_INVALID_RESPONSE: (
        "Translation provider returned an invalid response",
        True,
    ),
    ErrorCode.PROVIDER_REFUSAL: ("Translation provider refused the request", False),
}


class DocumentError(Exception):
    """Safe document failure; raw library errors remain inside adapters."""

    def __init__(self, error_code: ErrorCode) -> None:
        self.error_code = error_code
        self.message, self.retryable = _CATALOG[error_code]
        super().__init__(self.message)


class ProviderError(Exception):
    """Catalogued failure, with known billed usage retained for attempt accounting.

    Zero usage on transport failures means unknown usage, not guaranteed free
    execution. Never include raw provider messages in this exception.
    """

    def __init__(
        self,
        error_code: ErrorCode,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.message, self.retryable = _CATALOG[error_code]
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.model = model
        super().__init__(self.message)
