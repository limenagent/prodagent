"""Atomic JSON / JSONL file helpers shared by every durable store."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9_.:\-]+$")


def safe_filename_component(s: str) -> str:
    if not s or not _SAFE_FILENAME.match(s):
        raise ValueError(f"{s!r} contains unsafe characters; allowed: [A-Za-z0-9_.:-]")
    return s


def write_atomic_json(
    path: Path,
    data: Any,
    *,
    fsync: bool = False,
    indent: int = 2,
) -> None:
    tmp = path.with_suffix(".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=indent)
    try:
        if fsync:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        else:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_jsonl(path: Path, *, skip_errors: bool = True) -> Iterator[Any]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            if skip_errors:
                logger.warning("[jsonl] skipping corrupt line in %s", path.name)
                continue
            raise
