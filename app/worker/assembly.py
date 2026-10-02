"""Cache-driven final document rendering for a translated job."""

import asyncio
import os
import tempfile
from contextlib import suppress
from pathlib import Path

from app.adapters.persistence.worker import LeaseLostError, WorkerPersistence
from app.core.errors import DocumentError, ErrorCode
from app.core.models import Block, JobRecord, JobStatus
from app.core.ports import (
    DocumentRepository,
    FileStorage,
    FormatRegistry,
    JobExecutionRepository,
    TranslationCacheRepository,
)
from app.worker.keys import translation_key


class Assembly:
    """Render a job from persisted translations and store its final artifact."""

    def __init__(
        self,
        job_repo: JobExecutionRepository,
        cache_repo: TranslationCacheRepository,
        document_repo: DocumentRepository,
        format_registry: FormatRegistry,
        file_storage: FileStorage,
        *,
        persistence: WorkerPersistence,
    ) -> None:
        self._job_repo = job_repo
        self._cache_repo = cache_repo
        self._document_repo = document_repo
        self._format_registry = format_registry
        self._file_storage = file_storage
        self._persistence = persistence

    async def render(self, job: JobRecord) -> JobStatus:
        """Render from cache, publish the output, and persist the final status."""
        async with self._persistence.read():
            document = await self._document_repo.get_document(job.document_id)
            blocks = await self._document_repo.get_blocks(job.document_id)
            translations = await self._load_translations(job, blocks)

        if document is None:
            raise DocumentError(ErrorCode.RENDER_FAILED)

        status = (
            JobStatus.DONE
            if all(block.id in translations for block in blocks)
            else JobStatus.COMPLETED_WITH_ERRORS
        )
        output_path: Path | None = None
        try:
            original_path = await self._file_storage.get_upload_path(job.document_id)
            resolved = await self._format_registry.resolve(original_path)
            if resolved is None:
                raise DocumentError(ErrorCode.RENDER_FAILED)
            _, renderer = resolved
            output_path = await asyncio.to_thread(_create_temporary_output, original_path.suffix)
            rendered_path = await renderer.render(
                original_path,
                blocks,
                translations,
                output_path,
            )
            content = await asyncio.to_thread(Path.read_bytes, rendered_path)
            if job.lease_owner is None:
                raise LeaseLostError("job execution lease lost")
            async with self._persistence.read():
                await self._persistence.ensure_owned(job.id, job.lease_owner)
        except asyncio.CancelledError:
            raise
        except LeaseLostError:
            raise
        except Exception:
            raise DocumentError(ErrorCode.RENDER_FAILED) from None
        finally:
            if output_path is not None:
                with suppress(OSError):
                    await asyncio.to_thread(_remove_file, output_path)

        try:
            await self._file_storage.save_output(job.id, content, document.filename)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise DocumentError(ErrorCode.RENDER_FAILED) from None

        async with self._persistence.write():
            if job.lease_owner is None:
                raise LeaseLostError("job execution lease lost")
            await self._persistence.ensure_owned(job.id, job.lease_owner)
            await self._job_repo.complete_job(job.id, status)
        return status

    async def _load_translations(
        self,
        job: JobRecord,
        blocks: list[Block],
    ) -> dict[str, str]:
        key = translation_key(job)
        translations: dict[str, str] = {}
        for block in blocks:
            translated_text = await self._cache_repo.get_block_translation(key, block.id)
            if translated_text is not None:
                translations[block.id] = translated_text
        return translations


def _create_temporary_output(suffix: str) -> Path:
    descriptor, temporary_path = tempfile.mkstemp(prefix="document-translator-", suffix=suffix)
    os.close(descriptor)
    return Path(temporary_path)


def _remove_file(file_path: Path) -> None:
    file_path.unlink(missing_ok=True)
