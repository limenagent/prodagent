"""Spill — oversized tool results move to disk, context keeps the handle.

A result too large for the window is written to a per-agent spill directory
and replaced in the transcript by a bounded ``<spilled>`` placeholder
carrying the path; ``read_tool_result`` is the way back in (grep + paging).
Path safety is structural: resolution is confined to the store's directory,
so a model-supplied path can only ever name a spilled file."""

from __future__ import annotations

import hashlib
import itertools
import logging
import re
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.cognition.context.budget import TokenCounter

logger = logging.getLogger(__name__)

__all__ = ["ToolResultSpillStore", "SpilledResult", "extract_spilled_path"]

_PREVIEW_BYTES = 2000
_SPILLED_PATH_RE = re.compile(r"<spilled\b[^>]*\bpath='([^']+)'")


def extract_spilled_path(content: str) -> str | None:
    """Pull the path back out of a ``<spilled>`` placeholder — how tooling
    recognizes "already spilled, nothing left to shrink" downstream."""
    if not content or not content.lstrip().startswith("<spilled"):
        return None
    m = _SPILLED_PATH_RE.search(content)
    return m.group(1) if m else None


class SpilledResult:
    __slots__ = ("call_id", "tool_name", "path", "original_chars", "original_tokens")

    def __init__(
        self,
        *,
        call_id: str,
        tool_name: str,
        path: Path,
        original_chars: int,
        original_tokens: int,
    ) -> None:
        self.call_id = call_id
        self.tool_name = tool_name
        self.path = path
        self.original_chars = original_chars
        self.original_tokens = original_tokens

    def placeholder(self, preview_chars: int = _PREVIEW_BYTES) -> str:
        """What stays in context after the spill: provenance header, a
        head-bounded preview, and the standing instruction to page with
        ``read_tool_result`` — the preview is a teaser, not the data."""
        preview = _truncate_at_boundary(_read_head(self.path, preview_chars), preview_chars)
        return (
            f"<spilled tool={self.tool_name!r} call_id={self.call_id!r} "
            f"path={str(self.path)!r} original_tokens={self.original_tokens} "
            f"original_chars={self.original_chars}>\n"
            f"Preview (first {preview_chars} chars):\n{preview}\n"
            f"</spilled>\n"
            f"Use the read_tool_result tool to query this file: pass `path` and a "
            f"`grep_pattern` to find specific entries. Do not rely on the preview - "
            f"it shows only the first entries."
        )


class ToolResultSpillStore:
    """Spill dir created lazily on first spill; per-process temp when None."""

    def __init__(
        self,
        spill_dir: Path | str | None = None,
        *,
        counter: TokenCounter | None = None,
    ) -> None:
        self._dir = Path(spill_dir) if spill_dir is not None else None
        self._counter = counter
        self._created = False
        self._spill_counter = itertools.count(1)
        self._dir_lock = threading.Lock()
        self.spill_count = 0

    @property
    def dir(self) -> Path:
        if self._dir is None:
            with self._dir_lock:
                if self._dir is None:
                    self._dir = Path(tempfile.mkdtemp(prefix="prodagent-spill-"))
        if not self._created:
            with self._dir_lock:
                if not self._created:
                    self._dir.mkdir(parents=True, exist_ok=True)
                    self._created = True
        return self._dir

    def spill(
        self,
        *,
        content: str,
        call_id: str,
        tool_name: str,
    ) -> SpilledResult:
        """Write once per call_id (``x`` mode — replays don't rewrite), name
        derived from the call so resume finds the same file."""
        path = self.dir / f"{_safe_name(call_id)}.txt"
        if not path.exists():
            try:
                with open(path, "x", encoding="utf-8") as fh:
                    fh.write(content)
            except FileExistsError:
                pass
        original_tokens = self._count(content)
        logger.info(
            "[spill] tool=%s call_id=%s -> %s (%d chars, ~%d tokens)",
            tool_name,
            call_id,
            path,
            len(content),
            original_tokens,
        )
        self.spill_count = next(self._spill_counter)
        return SpilledResult(
            call_id=call_id,
            tool_name=tool_name,
            path=path,
            original_chars=len(content),
            original_tokens=original_tokens,
        )

    def resolve(self, path: str | Path) -> Path:
        """Confine ``path`` to the spill directory; refuse symlinks and escapes."""
        raw = Path(path)
        if not raw.is_absolute():
            # Relative model-supplied paths: take only the final component —
            # "sub/dir/escape.txt" becomes "escape.txt", still inside.
            raw = self.dir / raw.name
        if raw.is_symlink():
            # Defence 1: a symlink planted in the spill dir could point anywhere.
            raise ValueError(f"refusing to follow symlink in spill dir: {path!r}")
        target = raw.resolve()
        base = self.dir.resolve()
        try:
            # Defence 2: after resolving everything, the result must still be
            # under the spill dir (catches ".." and nested tricks).
            target.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"refusing to read path outside spill dir: {path!r}") from exc
        return target

    def read_raw(self, path: str | Path) -> str | None:
        """Resolved read for ``read_tool_result`` — the only doorway back
        into a spilled payload."""
        resolved = self.resolve(path)
        if not resolved.exists():
            return None
        return resolved.read_text(encoding="utf-8", errors="replace")

    def _count(self, text: str) -> int:
        if self._counter is not None:
            return self._counter.count(text)
        return max(1, len(text) // 4)


def _safe_name(name: str) -> str:
    digest = hashlib.blake2b(name.encode(), digest_size=8).hexdigest()
    keep = []
    for ch in name:
        if ch.isalnum() or ch in ("-", "_", "."):
            keep.append(ch)
        else:
            keep.append("_")
    safe = "".join(keep)[:32]
    return f"{safe}_{digest}" if safe else f"result_{digest}"


def _truncate_at_boundary(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut == -1 or cut < limit // 2:
        cut = limit
    return text[:cut] + " ..."


def _read_head(path: Path, limit: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit + 1)
    except OSError:
        return ""
