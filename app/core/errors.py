"""Safe catalogued failures shared by adapters and the future worker."""

from enum import StrEnum


class ErrorCode(StrEnum):
    INTERNAL_ERROR = "internal_error"
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    ANALYSIS_PENDING = "analysis_pending"
    UNSUPPORTED_FORMAT = "unsupported_format"
    FILE_TOO_LARGE = "size_limit"
    PAGE_LIMIT = "page_limit"
    TEXT_LIMIT = "text_limit"
    NOT_READY = "not_ready"
    SCANNED_PDF = "scanned_pdf"
    CORRUPT_FILE = "corrupt_file"
    RENDER_FAILED = "render_failed"
    COST_CAP_EXCEEDED = "cost_cap_exceeded"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_CONNECTION = "provider_connection"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_SERVER_ERROR = "provider_server_error"
    PROVIDER_BAD_REQUEST = "provider_bad_request"
    PROVIDER_AUTH_ERROR = "provider_auth_error"
    PROVIDER_INVALID_RESPONSE = "provider_invalid_response"
    PROVIDER_REFUSAL = "provider_refusal"


_CATALOG: dict[ErrorCode, tuple[str, bool]] = {
    ErrorCode.INTERNAL_ERROR: ("Internal server error", True),
    ErrorCode.INVALID_REQUEST: ("Request validation failed", False),
    ErrorCode.NOT_FOUND: ("Requested resource was not found", False),
    ErrorCode.ANALYSIS_PENDING: ("Document analysis is pending; retry shortly", True),
    ErrorCode.CONFLICT: ("Request conflicts with the resource state", False),
    ErrorCode.UNSUPPORTED_FORMAT: ("Document format is unsupported", False),
    ErrorCode.FILE_TOO_LARGE: ("Document exceeds the 50 MiB upload limit", False),
    ErrorCode.PAGE_LIMIT: ("Document exceeds the 400-page limit", False),
    ErrorCode.TEXT_LIMIT: ("Document exceeds the 10 MiB extracted-text limit", False),
    ErrorCode.NOT_READY: ("Service dependencies are unavailable", True),
    ErrorCode.SCANNED_PDF: ("PDF has no usable text layer; OCR is unsupported", False),
    ErrorCode.CORRUPT_FILE: ("Document cannot be read or is corrupt", False),
    ErrorCode.RENDER_FAILED: ("Translated document could not be rendered", False),
    ErrorCode.COST_CAP_EXCEEDED: ("Job translation cost limit reached", False),
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


def catalog_entry(error_code: str) -> tuple[str, bool]:
    """Return a safe message and retry policy, including for unknown stored codes."""
    try:
        code = ErrorCode(error_code)
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    return _CATALOG[code]


class ServiceError(Exception):
    """A catalogued application failure with its outward HTTP classification."""

    def __init__(self, error_code: ErrorCode, *, status_code: int = 422) -> None:
        self.error_code = error_code
        self.message, self.retryable = catalog_entry(error_code)
        self.status_code = status_code
        super().__init__(self.message)


class DocumentError(Exception):
    """Safe document failure; raw library errors remain inside adapters."""

    def __init__(self, error_code: ErrorCode) -> None:
        self.error_code = error_code
        self.message, self.retryable = _CATALOG[error_code]
        super().__init__(self.message)


class AnalysisPendingError(RuntimeError):
    """Internal signal that analysis changed before an atomic job insert."""


class ProviderError(Exception):
    """Catalogued failure, with known billed usage retained for attempt accounting.

    Zero usage on transport failures means unknown usage, not guaranteed free
    execution. Never include raw provider messages in this exception.
    """

    terminal: bool = False

    def __init__(
        self,
        error_code: ErrorCode,
        *,
        tokens_in: int = 0,
        tokens_out: int = 0,
        model: str | None = None,
        cached_tokens_in: int = 0,
        requests: int = 0,
    ) -> None:
        self.error_code = error_code
        self.message, self.retryable = _CATALOG[error_code]
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out
        self.model = model
        self.cached_tokens_in = cached_tokens_in
        self.requests = requests
        super().__init__(self.message)


class TriageTerminalError(ProviderError):
    """A provider failure that the triage service must not retry.

    The catalogued ``retryable`` value describes the error code generally;
    this flag marks a particular triage occurrence as terminal. Turn-budget
    exhaustion repeats the same prompt and model loop, while the bulk path
    continues to retry ``PROVIDER_INVALID_RESPONSE`` as usual.
    """

    terminal: bool = True
