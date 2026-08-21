from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import SideEffectLevel, ToolError, ToolMeta
from prodagent.tooling.builtin._atomic import check_path
from prodagent.tooling.builtin._fs_common import _DEFAULT_IGNORE_DIRS, normalize_allowed_dirs
from prodagent.tooling.decorator import tool

if TYPE_CHECKING:
    from collections.abc import Iterator

    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)

__all__ = ["make_grep"]

_MAX_MATCHES = 200
_MAX_FILE_SIZE = 10 * 1024 * 1024
_BINARY_EXTS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".bz2",
    ".7z",
    ".jar",
    ".class",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".o",
    ".a",
    ".pyc",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".wav",
    ".flac",
}

_GREP_DESCRIPTION = (
    "Search file contents with a regular expression. Returns matching lines "
    "with file paths and line numbers (file:line:content). Skips binary files "
    "and common ignored directories (.git, node_modules, __pycache__, etc.). "
    "Use glob to find files by name; use grep for content search."
)


def make_grep(allowed_dirs: list[Path] | None = None) -> FunctionTool:
    allowed = normalize_allowed_dirs(allowed_dirs)

    async def _grep(
        pattern: Annotated[
            str, Field(description="Regular expression to search for (Python re syntax).")
        ],
        path: Annotated[
            str, Field(description="File or directory to search. Defaults to current directory.")
        ] = ".",
        glob: Annotated[
            str | None,
            Field(description="Optional file-name pattern to restrict search (e.g. '*.py')."),
        ] = None,
        case_insensitive: Annotated[
            bool, Field(description="Case-insensitive match when true.")
        ] = False,
        max_results: Annotated[
            int, Field(description="Maximum number of matching lines to return.")
        ] = _MAX_MATCHES,
    ) -> dict[str, Any] | ToolError:
        base = Path(path).expanduser()
        resolved = check_path(base, allowed)
        if resolved is None:
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="path_not_allowed",
                message=f"Path '{path}' is outside allowed directories.",
                hint="Use a path under one of the allowed directories.",
            )
        base = resolved

        if not base.exists():
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="path_not_found",
                message=f"Search path '{path}' does not exist.",
                hint="Check the path and retry.",
            )

        flags = re.MULTILINE
        if case_insensitive:
            flags |= re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="invalid_pattern",
                message=f"Invalid regex pattern: {exc}",
                hint="Fix the pattern syntax and retry.",
            )

        cap = min(max_results, _MAX_MATCHES)
        matches: list[dict[str, Any]] = []
        files_scanned = 0

        files = [base] if base.is_file() else list(_iter_files(base, glob))

        for file_path in files:
            if len(matches) >= cap:
                break
            if _is_binary(file_path):
                continue
            try:
                if file_path.stat().st_size > _MAX_FILE_SIZE:
                    continue
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files_scanned += 1
            for line_num, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append(
                        {
                            "file": str(file_path),
                            "line": line_num,
                            "content": line.rstrip()[:500],
                        }
                    )
                    if len(matches) >= cap:
                        break

        formatted = "\n".join(f"{m['file']}:{m['line']}:{m['content']}" for m in matches)
        return {
            "content": formatted,
            "matches": matches,
            "count": len(matches),
            "truncated": len(matches) >= cap,
            "files_scanned": files_scanned,
            "pattern": pattern,
            "ok": True,
        }

    meta = ToolMeta(
        name="grep",
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        domain="fs:read",
        timeout_seconds=10.0,
    )
    return tool(_grep, name="grep", description=_GREP_DESCRIPTION, meta=meta)


def _iter_files(root: Path, name_glob: str | None) -> Iterator[Path]:
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in _DEFAULT_IGNORE_DIRS:
                    continue
                stack.append(entry)
                continue
            if name_glob and not _matches_glob(entry.name, name_glob):
                continue
            yield entry


def _matches_glob(name: str, pattern: str) -> bool:
    if "{" in pattern and "}" in pattern:
        prefix, rest = pattern.split("{", 1)
        alts, suffix = rest.split("}", 1)
        return any(fnmatch.fnmatch(name, prefix + alt + suffix) for alt in alts.split(","))
    return fnmatch.fnmatch(name, pattern)


def _is_binary(path: Path) -> bool:
    return path.suffix.lower() in _BINARY_EXTS
