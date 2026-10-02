"""Job creation, idempotency, and retry orchestration."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from app.config import Settings
from app.core.errors import AnalysisPendingError, ErrorCode, ServiceError
from app.core.models import (
    Block,
    ChunkBlockRecord,
    ChunkRecord,
    ChunkStatus,
    DocumentStatus,
    JobRecord,
    JobStatus,
    TranslationPlan,
)
from app.core.ports import (
    CostCalculator,
    DocumentRepository,
    JobExecutionRepository,
    TranslationCacheRepository,
)

_CHUNK_TOKEN_BUDGET = 1_000
_PROMPT_VERSION = "v1"


class JobServicePersistence(Protocol):
    """Internal persistence operations required by the service."""

    def read(self) -> AbstractAsyncContextManager[None]: ...

    def write(self) -> AbstractAsyncContextManager[None]: ...

    async def find_by_idempotency_key(self, key: str) -> JobRecord | None: ...

    async def get_batch_family(self, request_digest: str) -> list[JobRecord]: ...

    async def get_job(self, job_id: str) -> JobRecord | None: ...

    async def get_jobs_by_batch(self, batch_id: str) -> list[JobRecord]: ...

    async def retry_job(
        self,
        job_id: str,
        *,
        translation_key: str,
        raised_cost_cap_usd: float | None,
        default_cost_cap_usd: float,
        default_max_attempts: int,
    ) -> JobRecord | None: ...


class JobService:
    """Coordinate validated job creation and retry transitions."""

    def __init__(
        self,
        document_repo: DocumentRepository,
        job_repo: JobExecutionRepository,
        cache_repo: TranslationCacheRepository,
        cost_calculator: CostCalculator,
        *,
        persistence: JobServicePersistence,
        settings: Settings,
        model: str | None = None,
    ) -> None:
        self._document_repo = document_repo
        self._job_repo = job_repo
        self._cache_repo = cache_repo
        self._cost_calculator = cost_calculator
        self._persistence = persistence
        self._settings = settings
        self._model = model or settings.openai_model

    async def create_jobs(
        self,
        document_id: str,
        target_languages: list[str],
        idempotency_key: str,
    ) -> list[JobRecord]:
        """Create one idempotent job per distinct normalized target language."""
        languages = self._normalize_languages(target_languages)
        request_key = idempotency_key.strip()
        if not request_key:
            raise self._error(ErrorCode.INVALID_REQUEST, 422)
        if not document_id.strip():
            raise self._error(ErrorCode.INVALID_REQUEST, 422)

        request_digest = self._digest(request_key)
        language_digest = self._digest(json.dumps(sorted(languages), separators=(",", ":")))
        batch_id = f"{request_digest}.{language_digest}"

        existing_family = await self._persistence.get_batch_family(request_digest)
        if existing_family:
            self._validate_existing_family(
                existing_family, document_id, languages, batch_id, require_complete=False
            )

        async with self._persistence.read():
            document = await self._document_repo.get_document(document_id)
            if document is None:
                raise self._error(ErrorCode.NOT_FOUND, 404)
            if document.status is not DocumentStatus.EXTRACTED:
                raise self._error(ErrorCode.ANALYSIS_PENDING, 409)
            blocks = await self._document_repo.get_blocks(document_id)
            analysis = await self._document_repo.get_analysis(document_id)
            if analysis is None:
                raise self._error(ErrorCode.ANALYSIS_PENDING, 409)

        if existing_family and {job.target_language for job in existing_family} == set(languages):
            return self._order_jobs(existing_family, languages)

        if not blocks:
            chunks_of_blocks: list[list[Block]] = []
        else:
            chunks_of_blocks = await asyncio.to_thread(self._group_blocks, blocks, self._model)

        plan = TranslationPlan(
            source_language=analysis.source_language,
            domain=analysis.domain,
            register=analysis.register,
            terms=analysis.terms,
            warnings=analysis.warnings,
            triage_status=analysis.triage_status,
        )
        # Stage 5 has no target-language term renderer yet. Identity entries
        # preserve the source terms in the provider prompt without inventing
        # translations.
        source_terms = plan.terms
        created: list[JobRecord] = []
        for language in languages:
            idempotency_key_for_language = self._language_key(request_key, language)
            existing = await self._persistence.find_by_idempotency_key(idempotency_key_for_language)
            if existing is not None:
                if (
                    existing.document_id != document_id
                    or existing.target_language != language
                    or existing.batch_id != batch_id
                ):
                    raise self._error(ErrorCode.CONFLICT, 409)
                created.append(existing)
                continue

            now = datetime.now(UTC)
            job_id = str(uuid4())
            glossary = {term: term for term in source_terms}
            chunk_records: list[ChunkRecord] = []
            chunk_links: list[ChunkBlockRecord] = []
            for chunk_seq, grouped_blocks in enumerate(chunks_of_blocks):
                chunk_id = str(uuid4())
                chunk_records.append(
                    ChunkRecord(
                        id=chunk_id,
                        job_id=job_id,
                        seq=chunk_seq,
                        status=ChunkStatus.PENDING,
                        lease_owner=None,
                        lease_expires_at=None,
                        created_at=now,
                    )
                )
                chunk_links.extend(
                    ChunkBlockRecord(
                        chunk_id=chunk_id,
                        block_id=block.id,
                        seq_in_chunk=seq_in_chunk,
                    )
                    for seq_in_chunk, block in enumerate(grouped_blocks)
                )

            job = JobRecord(
                id=job_id,
                document_id=document_id,
                batch_id=batch_id,
                target_language=language,
                status=JobStatus.QUEUED,
                total_chunks=len(chunk_records),
                done_chunks=0,
                model=self._model,
                prompt_version=_PROMPT_VERSION,
                glossary=glossary,
                tokens_in=0,
                tokens_out=0,
                cost_usd=0.0,
                error_code=None,
                error_detail=None,
                idempotency_key=idempotency_key_for_language,
                lease_owner=None,
                lease_expires_at=None,
                created_at=now,
                updated_at=now,
            )
            try:
                await self._job_repo.create_job_with_chunks(job, chunk_records, chunk_links)
            except AnalysisPendingError:
                raise self._error(ErrorCode.ANALYSIS_PENDING, 409) from None
            except RuntimeError as exc:
                raise self._error(ErrorCode.CONFLICT, 409) from exc
            persisted = await self._persistence.find_by_idempotency_key(
                idempotency_key_for_language
            )
            if persisted is None:
                raise self._error(ErrorCode.INTERNAL_ERROR, 500)
            if (
                persisted.document_id != document_id
                or persisted.target_language != language
                or persisted.batch_id != batch_id
            ):
                raise self._error(ErrorCode.CONFLICT, 409)
            created.append(persisted)

        # Detect a concurrent request with the same raw key and another set.
        complete_family = await self._persistence.get_batch_family(request_digest)
        self._validate_existing_family(
            complete_family, document_id, languages, batch_id, require_complete=True
        )
        return self._order_jobs(complete_family or created, languages)

    async def get_job(self, job_id: str) -> JobRecord | None:
        return await self._persistence.get_job(job_id)

    async def get_jobs_by_batch(self, batch_id: str) -> list[JobRecord]:
        return await self._persistence.get_jobs_by_batch(batch_id)

    async def retry_job(
        self,
        job_id: str,
        raised_cost_cap_usd: float | None = None,
    ) -> JobRecord | None:
        job = await self._persistence.get_job(job_id)
        if job is None:
            return None
        if job.status not in {JobStatus.FAILED, JobStatus.COMPLETED_WITH_ERRORS}:
            raise self._error(ErrorCode.CONFLICT, 409)
        try:
            async with self._persistence.write():
                updated = await self._persistence.retry_job(
                    job_id,
                    translation_key=self._translation_key(job),
                    raised_cost_cap_usd=raised_cost_cap_usd,
                    default_cost_cap_usd=self._settings.max_cost_per_job_usd,
                    default_max_attempts=self._settings.max_chunk_attempts,
                )
        except ValueError as exc:
            raise self._error(ErrorCode.INVALID_REQUEST, 422) from exc
        except RuntimeError as exc:
            raise self._error(ErrorCode.CONFLICT, 409) from exc
        return updated

    @staticmethod
    def _normalize_languages(target_languages: Sequence[str]) -> list[str]:
        languages: list[str] = []
        seen: set[str] = set()
        for value in target_languages:
            if not isinstance(value, str):
                raise JobService._error(ErrorCode.INVALID_REQUEST, 422)
            language = "-".join(part for part in value.strip().replace("_", "-").split("-") if part)
            language = language.casefold()
            if not language:
                raise JobService._error(ErrorCode.INVALID_REQUEST, 422)
            if language not in seen:
                seen.add(language)
                languages.append(language)
        if not languages:
            raise JobService._error(ErrorCode.INVALID_REQUEST, 422)
        return languages

    @staticmethod
    def _group_blocks(blocks: Sequence[Block], model: str) -> list[list[Block]]:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("o200k_base")

        ordered = sorted(blocks, key=lambda block: block.seq)
        chunks: list[list[Block]] = []
        current: list[Block] = []
        current_tokens = 0
        for block in ordered:
            token_count = len(encoding.encode(block.source_text, disallowed_special=()))
            if current and current_tokens + token_count > _CHUNK_TOKEN_BUDGET:
                chunks.append(current)
                current = []
                current_tokens = 0
            current.append(block)
            current_tokens += token_count
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _validate_existing_family(
        family: Sequence[JobRecord],
        document_id: str,
        languages: Sequence[str],
        batch_id: str,
        *,
        require_complete: bool,
    ) -> None:
        if any(job.document_id != document_id or job.batch_id != batch_id for job in family):
            raise JobService._error(ErrorCode.CONFLICT, 409)
        found_languages = {job.target_language for job in family}
        if not found_languages.issubset(set(languages)):
            raise JobService._error(ErrorCode.CONFLICT, 409)
        if require_complete and found_languages != set(languages):
            raise JobService._error(ErrorCode.CONFLICT, 409)

    @staticmethod
    def _order_jobs(jobs: Sequence[JobRecord], languages: Sequence[str]) -> list[JobRecord]:
        by_language = {job.target_language: job for job in jobs}
        return [by_language[language] for language in languages if language in by_language]

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @classmethod
    def _language_key(cls, request_key: str, language: str) -> str:
        request_digest = cls._digest(request_key)
        language_digest = cls._digest(f"{request_key}\0{language}")
        return f"{request_digest}.{language_digest}"

    @classmethod
    def _translation_key(cls, job: JobRecord) -> str:
        glossary_hash = cls._digest(
            json.dumps(job.glossary, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        )
        return cls._digest(
            json.dumps(
                [job.target_language, job.model, job.prompt_version, glossary_hash],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    @staticmethod
    def _error(code: ErrorCode, status_code: int) -> ServiceError:
        return ServiceError(code, status_code=status_code)
