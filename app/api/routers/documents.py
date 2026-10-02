"""Thin REST endpoint for document uploads."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.dependencies import get_document_service
from app.api.schemas import DocumentUploadResponse
from app.core.errors import ErrorCode, ServiceError
from app.core.services.document_service import MAX_UPLOAD_BYTES, DocumentService

router = APIRouter(prefix="/api/documents", tags=["documents"])
_UPLOAD_READ_SIZE = 64 * 1024


@router.post("", response_model=DocumentUploadResponse)
async def upload_document(
    file: Annotated[UploadFile, File()],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentUploadResponse:
    """Read an upload in bounded chunks and delegate document handling."""
    try:
        content = await _read_bounded_upload(file)
        document, block_count = await document_service.upload(file.filename or "", content)
        return DocumentUploadResponse(
            id=document.id,
            filename=document.filename,
            format=document.format,
            status=document.status,
            block_count=block_count,
        )
    finally:
        await file.close()


async def _read_bounded_upload(file: UploadFile) -> bytes:
    content = bytearray()
    while True:
        remaining = MAX_UPLOAD_BYTES + 1 - len(content)
        chunk = await file.read(min(_UPLOAD_READ_SIZE, remaining))
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > MAX_UPLOAD_BYTES:
            raise ServiceError(ErrorCode.FILE_TOO_LARGE, status_code=413)
    return bytes(content)
