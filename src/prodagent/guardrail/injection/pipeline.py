"""Five trust-boundary guardrail pipeline — mechanism only, policy injected.

Detection patterns live in :class:`InjectionPolicy` and are supplied by the
application. With no patterns configured every layer passes content through
unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.core.exceptions import PromptInjectionDetected
from prodagent.guardrail.injection.policy import InjectionPolicy, OutputDisposition

if TYPE_CHECKING:
    import re
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


def scan_for_injection(
    text: str,
    source: str = "input",
    patterns: Sequence[re.Pattern[str]] = (),
) -> None:
    """Raise ``PromptInjectionDetected`` on the first pattern match.

    With no patterns (the default) this never raises — detection is opt-in.
    """
    for pattern in patterns:
        if pattern.search(text):
            logger.warning("Injection pattern detected in %s: %s", source, pattern.pattern[:60])
            raise PromptInjectionDetected(
                f"Prompt injection detected in {source}",
                source=source,
                pattern=pattern.pattern,
            )


def scan_for_pii(
    text: str,
    patterns: Sequence[re.Pattern[str]] = (),
) -> list[str]:
    """Return snippets of the sensitive-content patterns that matched."""
    return [p.pattern[:40] for p in patterns if p.search(text)]


@dataclass
class GuardrailPipeline:
    policy: InjectionPolicy = field(default_factory=InjectionPolicy)

    # Ad-hoc scan with the policy's injection patterns (e.g. tool results).
    def scan_text(self, text: str, source: str = "input") -> None:
        scan_for_injection(text, source=source, patterns=self.policy.injection_patterns)

    # L1: raw user input
    def filter_input(self, user_input: str) -> str:
        self.scan_text(user_input, source="user_input")
        return user_input

    # L2: RAG results — split into clean/quarantined, don't silently drop
    def filter_retrieved_context(self, docs: list[str]) -> tuple[list[str], list[str]]:
        clean: list[str] = []
        quarantined: list[str] = []
        for doc in docs:
            try:
                self.scan_text(doc, source="rag_result")
                clean.append(doc)
            except PromptInjectionDetected:
                logger.warning("RAG document quarantined due to injection pattern")
                quarantined.append(doc)
        return clean, quarantined

    # L3: conversation turns
    def filter_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for msg in messages:
            if msg.get("role") == "system":
                continue
            content = str(msg.get("content", ""))
            self.scan_text(content, source="conversation_turn")
        return messages

    # L4: tool parameter poisoning
    def filter_tool_args(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        self._scan_tool_arg(tool_name, "", args)
        return args

    def _scan_tool_arg(self, tool_name: str, path: str, value: Any) -> None:
        if isinstance(value, str):
            self.scan_text(value, source=f"tool_arg:{tool_name}{path}")
        elif isinstance(value, dict):
            for k, v in value.items():
                self._scan_tool_arg(tool_name, f"{path}.{k}", v)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self._scan_tool_arg(tool_name, f"{path}[{i}]", item)

    # L5: output sensitive content — disposition comes from the policy
    def filter_output(self, response_text: str) -> tuple[str, list[str]]:
        findings = scan_for_pii(response_text, patterns=self.policy.sensitive_patterns)
        if findings:
            logger.warning("Sensitive content detected in model output: %s", findings)
        if not findings or self.policy.output_disposition is not OutputDisposition.REDACT:
            return response_text, findings
        redacted = response_text
        for pattern in self.policy.sensitive_patterns:
            redacted = pattern.sub(self.policy.redaction_placeholder, redacted)
        return redacted, findings
