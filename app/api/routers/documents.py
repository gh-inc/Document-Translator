"""Thin REST endpoints for document uploads and status."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Request, UploadFile

from app.adapters.llm.triage_runtime import ClaimedTriage, prepare_triage
from app.api.dependencies import TriageClaimsDependency, get_document_service
from app.api.schemas import DocumentUploadResponse
from app.core.errors import ErrorCode, ServiceError
from app.core.models import DocumentRecord, DocumentStatus
from app.core.services.document_service import MAX_UPLOAD_BYTES, DocumentService

router = APIRouter(prefix="/api/documents", tags=["documents"])
_UPLOAD_READ_SIZE = 64 * 1024


@router.get("/{document_id}", response_model=DocumentUploadResponse)
async def get_document(
    document_id: str,
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentUploadResponse:
    """Read persisted document readiness without scheduling analysis."""
    document, block_count, analysis = await document_service.get_document(document_id)
    return DocumentUploadResponse(
        id=document.id,
        filename=document.filename,
        format=document.format,
        status=document.status,
        block_count=block_count,
        analysis_cost_usd=analysis.cost_usd_total if analysis is not None else 0.0,
    )


@router.post("", response_model=DocumentUploadResponse)
async def upload_document(
    request: Request,
    background_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File()],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    triage_claims: TriageClaimsDependency,
) -> DocumentUploadResponse:
    """Read an upload in bounded chunks and delegate document handling."""
    try:
        content = await _read_bounded_upload(file)
        result = await document_service.upload(file.filename or "", content)
        document = result.document
        await _schedule_triage(request, background_tasks, document, triage_claims)
        return DocumentUploadResponse(
            id=document.id,
            filename=document.filename,
            format=document.format,
            status=document.status,
            block_count=result.block_count,
            analysis_cost_usd=result.analysis_cost_usd,
            warnings=result.warnings,
        )
    finally:
        await file.close()


@router.post("/{document_id}/retry-triage", response_model=DocumentUploadResponse)
async def retry_triage(
    document_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    triage_claims: TriageClaimsDependency,
) -> DocumentUploadResponse:
    document, block_count, analysis = await document_service.retry_triage(document_id)
    await _schedule_triage(request, background_tasks, document, triage_claims)
    return DocumentUploadResponse(
        id=document.id,
        filename=document.filename,
        format=document.format,
        status=document.status,
        block_count=block_count,
        analysis_cost_usd=analysis.cost_usd_total if analysis is not None else 0.0,
    )


async def _schedule_triage(
    request: Request,
    background_tasks: BackgroundTasks,
    document: DocumentRecord,
    claims: list[ClaimedTriage],
) -> None:
    if document.status is DocumentStatus.ANALYZING:
        claimed = await prepare_triage(
            document.id,
            request.app.state.settings,
            request.app.state.triage_agent_factory,
        )
        if claimed is not None:
            claims.append(claimed)
            try:
                background_tasks.add_task(claimed.run)
            except BaseException:
                await claimed.close()
                raise


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
