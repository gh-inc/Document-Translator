"""Map application and framework failures to the approved safe error envelope."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.api.schemas import ErrorResponse
from app.core.errors import DocumentError, ErrorCode, ProviderError, ServiceError, catalog_entry


def error_response(code: ErrorCode, status_code: int) -> JSONResponse:
    message, retryable = catalog_entry(code)
    body = ErrorResponse(error_code=code.value, message=message, retryable=retryable)
    return JSONResponse(status_code=status_code, content=body.model_dump())


async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    return error_response(exc.error_code, exc.status_code)


async def document_error_handler(request: Request, exc: DocumentError) -> JSONResponse:
    return error_response(exc.error_code, 422)


async def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
    return error_response(exc.error_code, 503 if exc.retryable else 422)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # Validation details can contain user data; the public envelope uses the catalog.
    return error_response(ErrorCode.INVALID_REQUEST, 422)


async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.INVALID_REQUEST
    return error_response(code, exc.status_code)


async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return error_response(ErrorCode.INTERNAL_ERROR, 500)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ServiceError, service_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(DocumentError, document_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(ProviderError, provider_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unexpected_error_handler)
