from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def check_path(path: Path, allowed: list[Path]) -> Path | None:
    resolved = path.resolve(strict=False)
    if not allowed:
        return resolved
    for root in allowed:
        if resolved == root or resolved.is_relative_to(root):
            return resolved
    return None


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = _default_file_mode()
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        mode="w",
        encoding=encoding,
        delete=False,
    )
    tmp_path = Path(tmp.name)
    try:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(Exception):
            tmp.close()
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _default_file_mode() -> int:
    """Mode a plain `open(path, 'w')` would produce, respecting umask."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
