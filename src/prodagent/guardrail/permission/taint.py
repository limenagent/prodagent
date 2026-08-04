"""Data flow taint tracking — keep sensitive context away from unauthorised tools."""

from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from prodagent.guardrail import patterns

if TYPE_CHECKING:
    from prodagent.core.types import ToolMeta
    from prodagent.tooling.registry import ToolRegistry

logger = logging.getLogger(__name__)


class TaintLevel(StrEnum):
    PUBLIC = "public"
    RESTRICTED = "restricted"  # PII present — exfiltration tools blocked
    SENSITIVE = "sensitive"  # secrets present — hard block on all network tools


def _default_meta() -> ToolMeta:
    from prodagent.core.types import ToolMeta

    return ToolMeta(name="")


class ContextTaintMonitor:
    """Per-run taint tracker."""

    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self.taint: TaintLevel = TaintLevel.PUBLIC
        self._pii_re = patterns.compile_patterns(patterns.PII_PATTERNS)
        self._secret_re = patterns.compile_patterns(patterns.SECRET_PATTERNS, flags=re.IGNORECASE)
        self._registry = tool_registry
        self._session_active: bool = False

    def _get_meta(self, tool_name: str) -> ToolMeta:
        if self._registry is not None and tool_name in self._registry:
            return self._registry.get_meta(tool_name)
        return _default_meta()

    def _blocked_tool_names(self) -> list[str]:
        if self._registry is None:
            return []
        return sorted(
            name
            for name in self._registry.names
            if self._registry.get_meta(name).is_exfiltration_tool
        )

    def on_tool_return(self, result: Any, meta: ToolMeta) -> None:
        result_str = str(result)

        if meta.produces_secrets or self._has_secrets(result_str):
            if self.taint != TaintLevel.SENSITIVE:
                blocked = self._blocked_tool_names()
                suffix = f" — blocked tools: {blocked}" if blocked else ""
                logger.warning("[ContextTaintMonitor] Context escalated to SENSITIVE%s", suffix)
            self.taint = TaintLevel.SENSITIVE
            return

        if meta.produces_pii or self._has_pii(result_str):
            if self.taint == TaintLevel.PUBLIC:
                blocked = self._blocked_tool_names()
                suffix = f" — blocked tools: {blocked}" if blocked else ""
                logger.warning("[ContextTaintMonitor] Context escalated to RESTRICTED%s", suffix)
            if self.taint != TaintLevel.SENSITIVE:
                self.taint = TaintLevel.RESTRICTED

    def check_before_call(self, tool_name: str, meta: ToolMeta) -> None:
        from prodagent.core.exceptions import DataFlowBlocked

        if (
            self.taint in (TaintLevel.RESTRICTED, TaintLevel.SENSITIVE)
            and meta.is_exfiltration_tool
        ):
            raise DataFlowBlocked(
                f"[ContextTaintMonitor] taint={self.taint.value} — "
                f"exfiltration tool '{tool_name}' is blocked. "
                "Each call was individually valid; the combination is lethal.",
                tool=tool_name,
                taint=self.taint.value,
            )

    def begin_session(self) -> None:
        if self._session_active:
            raise RuntimeError(
                "session already active — call end_session() before starting a new one"
            )
        self._session_active = True
        self._reset_for_session()

    def end_session(self) -> None:
        self._session_active = False

    def _reset_for_session(self) -> None:
        if self.taint != TaintLevel.PUBLIC:
            logger.debug("[ContextTaintMonitor] Taint reset to PUBLIC")
        self.taint = TaintLevel.PUBLIC

    def _has_pii(self, text: str) -> bool:
        return any(p.search(text) for p in self._pii_re)

    def _has_secrets(self, text: str) -> bool:
        return any(p.search(text) for p in self._secret_re)
