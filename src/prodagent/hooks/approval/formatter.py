"""Context-aware approval message formatter."""

from __future__ import annotations

import difflib
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.core.types import ToolCall

logger = logging.getLogger(__name__)

_MAX_DIFF_LINES = 40
_MAX_PARAM_CHARS = 200


class ContextAwareApprovalFormatter:
    def format(
        self,
        call: ToolCall,
        *,
        old_content: str | None = None,
        new_content: str | None = None,
        affected_count: int = 0,
        environment: str = "unknown",
    ) -> str:
        parts: list[str] = [
            "[APPROVAL REQUIRED]",
            f"Tool       : {call.name}",
        ]

        if old_content is not None and new_content is not None:
            diff = self._unified_diff(old_content, new_content)
            parts += [
                "Change diff:",
                diff if diff else "  (no textual change detected)",
            ]

        if affected_count:
            env_warn = (
                f"  WARNING: {affected_count} record(s) in PRODUCTION"
                if environment.lower() in ("prod", "production")
                else f"  {affected_count} record(s)"
            )
            parts.append(f"Impact     :{env_warn}")

        # Never dump full JSON
        safe_params = self._safe_params(call.params)
        parts.append(f"Parameters : {safe_params}")

        return "\n".join(p for p in parts if p)

    @staticmethod
    def _unified_diff(old: str, new: str) -> str:
        lines_old = old.splitlines(keepends=False)
        lines_new = new.splitlines(keepends=False)
        diff = difflib.unified_diff(lines_old, lines_new, lineterm="")
        return "\n".join(list(diff)[:_MAX_DIFF_LINES])

    @staticmethod
    def _safe_params(params: dict[str, Any]) -> str:
        try:
            raw = json.dumps(params, ensure_ascii=False)
            if len(raw) <= _MAX_PARAM_CHARS:
                return raw
            return raw[: _MAX_PARAM_CHARS - 3] + "..."
        except (TypeError, ValueError):
            return str(params)[:_MAX_PARAM_CHARS]
