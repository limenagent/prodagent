from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolName
    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RESULTS = 3


@dataclass
class ToolSearchConfig:
    weight_name_exact_part: float = 10.0
    weight_name_contains: float = 5.0
    weight_name_fallback: float = 2.0
    weight_description_word_match: float = 2.0
    weight_domain_match: float = 3.0


def preset_procedural() -> ToolSearchConfig:
    return ToolSearchConfig(
        weight_name_exact_part=12.0,
        weight_name_contains=6.0,
        weight_name_fallback=3.0,
        weight_description_word_match=1.5,
        weight_domain_match=3.0,
    )


@dataclass
class _ToolNameTokens:
    parts: list[str]
    normalized: str


@dataclass
class _ToolScore:
    tool: FunctionTool
    total_score: float


_SPECIAL_PREFIXES = ("mcp__", "agent__", "skill__", "subtool__")


class ToolNameParser:
    @classmethod
    def parse(cls, name: ToolName) -> _ToolNameTokens:
        for prefix in _SPECIAL_PREFIXES:
            if name.startswith(prefix):
                normalized = name.replace("__", " ").replace("_", " ")
                special_parts = normalized.lower().split()
                return _ToolNameTokens(parts=special_parts, normalized=normalized)

        parts: list[str] = []
        segment = ""
        for i, ch in enumerate(name):
            if ch == "_":
                if segment:
                    parts.append(segment)
                segment = ""
            elif ch.isupper() and i > 0:
                next_is_lower = i + 1 < len(name) and name[i + 1].islower()
                if segment and next_is_lower:
                    parts.append(segment)
                    segment = ch.lower()
                elif not segment:
                    segment = ch.lower()
                else:
                    segment += ch.lower()
            else:
                segment += ch.lower()
        if segment:
            parts.append(segment)

        normalized = " ".join(p.capitalize() for p in parts)
        return _ToolNameTokens(parts=parts, normalized=normalized)


class ToolDescriptionIndex:
    def __init__(self, tools: list[FunctionTool]) -> None:
        self._descriptions = {t.name: str(t.schema.get("description", "")).lower() for t in tools}

    def score(
        self,
        tool: FunctionTool,
        query_terms: list[str],
        config: ToolSearchConfig,
    ) -> float:
        desc = self._descriptions.get(tool.name, "")
        if not desc:
            return 0.0
        return sum(config.weight_description_word_match for term in query_terms if term in desc)


def _parse_tool_name(name: ToolName) -> _ToolNameTokens:
    return ToolNameParser.parse(name)


class ToolSearchIndex:
    def __init__(
        self,
        tools: list[FunctionTool],
        config: ToolSearchConfig | None = None,
    ) -> None:
        self._tools = {t.name: t for t in tools}
        self._config = config or ToolSearchConfig()
        self._name_tokens = {name: _parse_tool_name(name) for name in self._tools}
        self._desc_index = ToolDescriptionIndex(tools)
        logger.debug("ToolSearchIndex initialized with %d tools", len(tools))

    def search(self, query: str, max_results: int = _DEFAULT_MAX_RESULTS) -> list[FunctionTool]:
        if not self._tools or not query:
            return []

        query_lower = query.lower().strip()

        # Fast path: exact name or prefix match (single pass)
        for name, tool in self._tools.items():
            tokens = self._name_tokens[name]
            if name.lower() == query_lower or tokens.normalized.lower().startswith(query_lower):
                return [tool]

        query_terms = query_lower.split()
        if not query_terms:
            return []

        scored = [
            s
            for s in (self._score(t, query_terms) for t in self._tools.values())
            if s.total_score > 0
        ]
        scored.sort(key=lambda s: s.total_score, reverse=True)
        return [s.tool for s in scored[:max_results]]

    def _score(self, tool: FunctionTool, query_terms: list[str]) -> _ToolScore:
        tokens = self._name_tokens[tool.name]
        total = (
            self._score_name(tokens, query_terms)
            + self._desc_index.score(tool, query_terms, self._config)
            + self._score_domain(tool, query_terms)
        )
        return _ToolScore(tool=tool, total_score=total)

    def _score_name(self, tokens: _ToolNameTokens, query_terms: list[str]) -> float:
        cfg = self._config
        score = 0.0
        for term in query_terms:
            if term in tokens.parts:
                score += cfg.weight_name_exact_part
            elif any(term in part for part in tokens.parts):
                score += cfg.weight_name_contains
            elif term in tokens.normalized:
                score += cfg.weight_name_fallback
        return score

    def _score_domain(self, tool: FunctionTool, query_terms: list[str]) -> float:
        domain = (getattr(tool.meta, "domain", "") or "").lower()
        if not domain:
            return 0.0
        # bidirectional substring match: "database" matches term "db" if "db" in "database"
        return sum(
            self._config.weight_domain_match
            for term in query_terms
            if term in domain or domain in term
        )
