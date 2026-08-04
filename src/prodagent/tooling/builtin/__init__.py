from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.tooling.builtin._fs_common import normalize_allowed_dirs
from prodagent.tooling.builtin.edit import make_edit
from prodagent.tooling.builtin.glob import make_glob
from prodagent.tooling.builtin.grep import make_grep
from prodagent.tooling.builtin.read import make_read
from prodagent.tooling.builtin.read_tool_result import make_read_tool_result
from prodagent.tooling.builtin.write import make_builtin_fs_bundle, make_write

if TYPE_CHECKING:
    from pathlib import Path

    from prodagent.tooling.base import FunctionTool

__all__ = [
    "make_read",
    "make_edit",
    "make_write",
    "make_read_tool_result",
    "make_builtin_fs_bundle",
    "make_glob",
    "make_grep",
    "make_builtin_dev_bundle",
]


def make_builtin_dev_bundle(
    allowed_dirs: list[Path] | None = None,
) -> list[FunctionTool]:
    """Read + Edit + Write + Glob + Grep sharing one allowlist. Shell excluded — no real isolation."""
    read_tool, edit_tool, write_tool = make_builtin_fs_bundle(allowed_dirs=allowed_dirs)
    allowed = normalize_allowed_dirs(allowed_dirs)
    return [
        read_tool,
        edit_tool,
        write_tool,
        make_glob(allowed_dirs=allowed),
        make_grep(allowed_dirs=allowed),
    ]
