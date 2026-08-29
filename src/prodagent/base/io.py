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
    """Reject anything outside [A-Za-z0-9_.:-] — ids end up in file paths."""
    # run_ids / session_ids end up in file paths; this allowlist is the
    # path-traversal firewall — validate at the seam, not after an escape.
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
    """Persist ``data`` as JSON so a reader never sees a torn file."""
    # Write-temp-then-rename: POSIX rename is atomic, so a concurrent reader
    # sees either the whole old file or the whole new one — never a torn write.
    # ``fsync=True`` adds power-loss durability (data + parent dir, else the
    # rename itself may not survive); checkpoints pay for it, caches don't.
    tmp = path.with_suffix(".tmp")  # the staging file rename will replace
    # ensure_ascii=False keeps CJK readable on disk; indent trades bytes for
    # diffability — these are files humans grep, not wire packets.
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
    """Yield one parsed record per line — the JSONL reading contract."""
    # Corrupt lines are skipped by default: a store torn by a crash must lose
    # the partial tail, not refuse to load every record after it.
    #
    # Split on "\n" only, NOT str.splitlines(): JSON strings written with
    # ensure_ascii=False can carry U+0085/U+2028/U+2029 verbatim, and
    # splitlines() treats those as line breaks — silently corrupting a valid
    # JSON line into two unparseable halves (a skipped event). The writer
    # terminates lines with "\n" and nothing else; the reader must agree.
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").split("\n"):
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
