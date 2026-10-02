"""Resolve editor-supplied paths inside the dedicated shared directory."""

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path


def resolve_shared_path(root: Path, supplied_path: str, *, must_exist: bool) -> Path:
    resolved_root = root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise ValueError("configured MCP shared directory is not a directory")
    candidate = Path(supplied_path)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    resolved = candidate.resolve(strict=must_exist)
    if not resolved.is_relative_to(resolved_root):
        raise ValueError("path is outside the configured MCP shared directory")
    return resolved


def require_shared_file(root: Path, supplied_path: str) -> Path:
    resolved = resolve_shared_path(root, supplied_path, must_exist=True)
    if not resolved.is_file():
        raise ValueError("input path must be a regular file")
    return resolved


def ensure_shared_directory(root: Path, supplied_path: str) -> Path:
    with open_shared_directory(root, supplied_path, create=True) as (_descriptor, resolved):
        return resolved


@contextmanager
def open_shared_directory(
    root: Path, supplied_path: str, *, create: bool
) -> Iterator[tuple[int, Path]]:
    """Anchor I/O to an opened directory, rejecting symlink swaps at each step.

    The resolver permits existing symlinks contained in the mount. The resolved
    components are then opened relative to a directory descriptor with NOFOLLOW;
    swapping a component after resolution cannot redirect I/O outside the mount.
    Callers must perform reads/publication relative to the yielded descriptor.
    """
    resolved_root = root.resolve(strict=True)
    resolved = resolve_shared_path(root, supplied_path, must_exist=not create)
    descriptor = os.open(resolved_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in resolved.relative_to(resolved_root).parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o755, dir_fd=descriptor)
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor, resolved
    finally:
        os.close(descriptor)
