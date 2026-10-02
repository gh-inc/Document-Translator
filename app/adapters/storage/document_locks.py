"""Advisory document locks shared by independent REST and MCP processes."""

import asyncio
import fcntl
import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from app.adapters.persistence.database import _finish_cleanup


def _try_lock(database_path: Path, document_id: str, purpose: str) -> int | None:
    database_path = database_path.resolve()
    directory = database_path.parent / ".document-locks"
    directory.mkdir(parents=True, exist_ok=True)
    # Same physical volume may have different mount paths across containers.
    key = hashlib.sha256(f"{database_path.name}\0{document_id}\0{purpose}".encode()).hexdigest()
    descriptor = os.open(directory / key, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    # Never unlink: contenders must keep addressing the same inode.
    return descriptor


async def try_document_lock(database_path: Path, document_id: str, purpose: str) -> int | None:
    acquiring = asyncio.create_task(
        asyncio.to_thread(_try_lock, database_path, document_id, purpose)
    )
    try:
        return await asyncio.shield(acquiring)
    except BaseException:
        await _finish_cleanup(acquiring)
        descriptor = acquiring.result()
        if descriptor is not None:
            await release_document_lock(descriptor)
        raise


async def release_document_lock(descriptor: int) -> None:
    await _finish_cleanup(asyncio.to_thread(os.close, descriptor))


@asynccontextmanager
async def document_upload_lock(database_path: Path, document_id: str) -> AsyncIterator[None]:
    """Wait asynchronously for ingestion, releasing ownership on cancellation."""
    descriptor = None
    while descriptor is None:
        descriptor = await try_document_lock(database_path, document_id, "upload")
        if descriptor is None:
            await asyncio.sleep(0.02)
    try:
        yield
    finally:
        await release_document_lock(descriptor)
