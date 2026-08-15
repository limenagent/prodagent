"""Injection defence hook bundle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.core.exceptions import SensitiveContentDetected
from prodagent.guardrail.injection.policy import OutputDisposition
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
            self._pipeline.scan_text(str(result), source=f"tool_result:{name}")

    def scan_context(self, *, messages: list[Any] | None = None, **_: Any) -> None:
        if not messages:
            return
        self._pipeline.filter_messages(messages)

    def scan_tool_params(
        self, *, name: str = "", params: dict[str, Any] | None = None, **_: Any
    ) -> None:
        params = params or {}
        self._pipeline.filter_tool_args(name, params)

    def scan_output(self, *, final_output: str = "", run: Any = None, **_: Any) -> None:
        if not final_output:
            return
        clean, findings = self._pipeline.filter_output(final_output)
        if not findings:
            return
        disposition = self._pipeline.policy.output_disposition
        if disposition is OutputDisposition.VETO:
            raise SensitiveContentDetected(
                f"L5 output scan found {len(findings)} sensitive item(s) in final output",
                findings=findings[:5],
            )
        if disposition is OutputDisposition.REDACT and run is not None and clean != final_output:
            run.final_output = clean
            if getattr(run, "structured_output", None) is not None:
                # Parsed before redaction — now stale.
                run.structured_output = None
            logger.warning("L5 redacted %d sensitive item(s) from final output", len(findings))
        else:  # OBSERVE (or REDACT without a run object to write back to)
            logger.warning(
                "L5 output scan observed %d sensitive item(s): %s",
                len(findings),
                findings[:3],
            )

    def guard_kb_write(self, *, document: str = "", source: str = "unknown", **_: Any) -> None:
        if self._kb_guard and document:
            self._kb_guard.guard_document(document, source)

    def validate_handoff(self, *, handoff_data: dict[str, Any] | None = None, **_: Any) -> None:
        if handoff_data is None or self._handoff_actions is None:
            return
        from prodagent.guardrail.injection import validate_handoff_security

        validate_handoff_security(handoff_data, allowed_actions=self._handoff_actions)
