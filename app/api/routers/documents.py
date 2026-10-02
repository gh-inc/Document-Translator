"""Thin REST endpoint for document uploads."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Request, UploadFile

from app.api.background import run_triage
from app.api.dependencies import get_document_service
from app.api.schemas import DocumentUploadResponse
from app.core.errors import ErrorCode, ServiceError
from app.core.models import DocumentRecord, DocumentStatus
from app.core.services.document_service import MAX_UPLOAD_BYTES, DocumentService

router = APIRouter(prefix="/api/documents", tags=["documents"])
_UPLOAD_READ_SIZE = 64 * 1024


@router.post("", response_model=DocumentUploadResponse)
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentUploadResponse:
    """Read an upload in bounded chunks and delegate document handling."""
    try:
        content = await _read_bounded_upload(file)
        document, block_count = await document_service.upload(file.filename or "", content)
        _schedule_triage(request, background_tasks, document)
        return DocumentUploadResponse(
            id=document.id,
            filename=document.filename,
            format=document.format,
            status=document.status,
            block_count=block_count,
        )
    finally:
        await file.close()


@router.post("/{document_id}/retry-triage", response_model=DocumentUploadResponse)
async def retry_triage(
    document_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentUploadResponse:
    document, block_count = await document_service.retry_triage(document_id)
    _schedule_triage(request, background_tasks, document)
    return DocumentUploadResponse(
        id=document.id,
        filename=document.filename,
        format=document.format,
        status=document.status,
        block_count=block_count,
    )


def _schedule_triage(
    request: Request, background_tasks: BackgroundTasks, document: DocumentRecord
) -> None:
    if document.status is DocumentStatus.ANALYZING:
        lock = request.app.state.triage_locks.setdefault(document.id, asyncio.Lock())
        background_tasks.add_task(
            run_triage,
            document.id,
            request.app.state.settings,
            lock,
            request.app.state.triage_agent_factory,
        )


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
