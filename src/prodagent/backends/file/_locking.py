"""Filesystem locking helpers shared by file-backed stores."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

try:
    import fcntl as _fcntl

    def _flock_exclusive(fd: int) -> None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)

    def _funlock(fd: int) -> None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
except ImportError:  # Windows
    # fcntl is POSIX-only; on Windows the lock degrades to a no-op. File
    # backends there are single-process by contract — within one process,
    # asyncio's event loop already serializes the read-modify-write.
    def _flock_exclusive(fd: int) -> None:
        pass

    def _funlock(fd: int) -> None:
        pass


@contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """flock ``lock_path`` for the duration of a read-modify-write sequence."""
    lf = lock_path.open("a+", encoding="utf-8")
    try:
        _flock_exclusive(lf.fileno())
        yield
    finally:
        _funlock(lf.fileno())
        lf.close()
