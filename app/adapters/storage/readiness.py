"""Threaded storage readiness probe without retained temporary artifacts."""

import asyncio
import tempfile
from pathlib import Path


async def check_storage_writable(*paths: Path) -> None:
    await asyncio.to_thread(_probe_paths, paths)


def _probe_paths(paths: tuple[Path, ...]) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path) as probe:
            probe.write(b"ready")
            probe.flush()
