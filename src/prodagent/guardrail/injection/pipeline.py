"""Five trust-boundary guardrail pipeline."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from prodagent.core.exceptions import PromptInjectionDetected
from prodagent.guardrail import patterns

logger = logging.getLogger(__name__)


_COMPILED_INJECTION: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE | re.DOTALL) for p in patterns.INJECTION_PATTERNS
]
_COMPILED_PII: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE | re.DOTALL)
    for p in [*patterns.PII_PATTERNS, *patterns.SECRET_PATTERNS]
]


def scan_for_injection(
    text: str,
    source: str = "input",
    extra_patterns: list[re.Pattern[str]] | None = None,
) -> None:
    all_patterns = _COMPILED_INJECTION + (extra_patterns or [])
    for pattern in all_patterns:
        if pattern.search(text):
            logger.warning("Injection pattern detected in %s: %s", source, pattern.pattern[:60])
            raise PromptInjectionDetected(
                f"Prompt injection detected in {source}",
                source=source,
                pattern=pattern.pattern,
            )


def scan_for_pii(text: str) -> list[str]:
    found = []
    for pattern in _COMPILED_PII:
        if pattern.search(text):
            found.append(pattern.pattern[:40])
    return found


@dataclass
class GuardrailPipeline:
    extra_patterns: list[re.Pattern[str]] = field(default_factory=list)

    # L1: raw user input
    def filter_input(self, user_input: str) -> str:
        scan_for_injection(user_input, source="user_input", extra_patterns=self.extra_patterns)
        return user_input

    # L2: RAG results — split into clean/quarantined, don't silently drop
    def filter_retrieved_context(self, docs: list[str]) -> tuple[list[str], list[str]]:
        clean: list[str] = []
        quarantined: list[str] = []
        for doc in docs:
            try:
                scan_for_injection(doc, source="rag_result", extra_patterns=self.extra_patterns)
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
            scan_for_injection(
                content, source="conversation_turn", extra_patterns=self.extra_patterns
            )
        return messages

    # L4: tool parameter poisoning
    def filter_tool_args(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        self._scan_tool_arg(tool_name, "", args)
        return args

    def _scan_tool_arg(self, tool_name: str, path: str, value: Any) -> None:
        if isinstance(value, str):
            scan_for_injection(
                value,
                source=f"tool_arg:{tool_name}{path}",
                extra_patterns=self.extra_patterns,
            )
        elif isinstance(value, dict):
            for k, v in value.items():
                self._scan_tool_arg(tool_name, f"{path}.{k}", v)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self._scan_tool_arg(tool_name, f"{path}[{i}]", item)

    # L5: output PII
    def filter_output(self, response_text: str) -> tuple[str, list[str]]:
        findings = scan_for_pii(response_text)
        if findings:
            logger.warning("PII detected in model output: %s", findings)
        return response_text, findings
