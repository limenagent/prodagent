"""Injection defence hook bundle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.hooks.checkpoint import CheckPoint

if TYPE_CHECKING:
    from prodagent.guardrail.injection import (
        GuardrailPipeline,
        KnowledgeBaseWriteGuard,
    )
    from prodagent.hooks.registry import HookRegistry

logger = logging.getLogger(__name__)


class InjectionDefenseHooks:
    def __init__(
        self,
        *,
        pipeline: GuardrailPipeline,
        kb_guard: KnowledgeBaseWriteGuard | None = None,
        allowed_handoff_actions: frozenset[str] | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._kb_guard = kb_guard
        self._handoff_actions = allowed_handoff_actions

    def attach(self, hooks: HookRegistry) -> None:
        # L1-L4 scans at priority 50; L5 output/KB/handoff checks veto at 80.
        hooks.register_checker(CheckPoint.SESSION_START, self.scan_task, priority=50)
        hooks.register_checker(CheckPoint.TOOL_RESULT, self.scan_tool_result, priority=50)
        hooks.register_checker(CheckPoint.CONTEXT_BUILD, self.scan_context, priority=50)
        hooks.register_checker(CheckPoint.TOOL_CALL, self.scan_tool_params, priority=50)

        # L5 output scan must veto, not just observe.
        hooks.register_checker(CheckPoint.RUN_COMPLETE, self.scan_output, priority=80)

        if self._kb_guard:
            hooks.register_checker(CheckPoint.DOCUMENT_ADD, self.guard_kb_write, priority=80)

        if self._handoff_actions is not None:
            hooks.register_checker(CheckPoint.AGENT_HANDOFF, self.validate_handoff, priority=80)

    def scan_task(self, *, task: str = "", **_: Any) -> None:
        if task:
            self._pipeline.filter_input(task)

    def scan_tool_result(
        self, *, result: dict[str, Any] | None = None, name: str = "", **_: Any
    ) -> None:
        if result:
            from prodagent.guardrail.injection import scan_for_injection

            scan_for_injection(
                str(result),
                source=f"tool_result:{name}",
                extra_patterns=self._pipeline.extra_patterns,
            )

    def scan_context(self, *, messages: list[Any] | None = None, **_: Any) -> None:
        if not messages:
            return
        self._pipeline.filter_messages(messages)

    def scan_tool_params(
        self, *, name: str = "", params: dict[str, Any] | None = None, **_: Any
    ) -> None:
        params = params or {}
        self._pipeline.filter_tool_args(name, params)

    def scan_output(self, *, final_output: str = "", **_: Any) -> None:
        if not final_output:
            return
        _clean, findings = self._pipeline.filter_output(final_output)
        if findings:
            from prodagent.core.exceptions import PromptInjectionDetected

            raise PromptInjectionDetected(
                f"L5 output scan found {len(findings)} issue(s) in final output: {findings[:3]}"
            )

    def guard_kb_write(self, *, document: str = "", source: str = "unknown", **_: Any) -> None:
        if self._kb_guard and document:
            self._kb_guard.guard_document(document, source)

    def validate_handoff(self, *, handoff_data: dict[str, Any] | None = None, **_: Any) -> None:
        if handoff_data is None:
            return
        from prodagent.guardrail.injection import validate_handoff_security

        validate_handoff_security(handoff_data, allowed_actions=self._handoff_actions)
