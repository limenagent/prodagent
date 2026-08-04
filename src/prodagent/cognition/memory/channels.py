from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prodagent.cognition.memory.embedder import HashEmbedder, cosine
from prodagent.cognition.memory.storage import MemoryType

if TYPE_CHECKING:
    from prodagent.cognition.context.budget import TokenCounter
    from prodagent.cognition.memory.storage import StoredMemory

__all__ = [
    "RecalledItem",
    "RecallContext",
    "top_k_by_cosine",
    "RuleChannel",
    "ExactChannel",
    "SemanticChannel",
    "EntityChannel",
    "DEFAULT_MERGE_ORDER",
]


@dataclass
class RecalledItem:
    content: str
    recall_stage: str  # "rule_force" | "exact" | "semantic" | "entity"
    token_count: int
    source_mem: StoredMemory | None = None


@dataclass
class RecallContext:
    constraints: list[StoredMemory]
    documents: list[StoredMemory]
    facts: list[StoredMemory]
    static_constraints: list[str]
    counter: TokenCounter


DEFAULT_MERGE_ORDER = ("rule_force", "entity", "exact", "semantic")

_SNIPPET_MAX_CHARS = 250


def _keywords(query: str, *, min_len: int = 2) -> set[str]:
    from prodagent.core.text import tokenize_cjk

    return set(tokenize_cjk(query, min_len=min_len))


def top_k_by_cosine(
    query_vec: list[float],
    mems: list[StoredMemory],
    embedder: HashEmbedder,
    *,
    k: int,
    min_cosine: float,
    domain: str | None = None,
    exclude_id: str | None = None,
) -> list[tuple[float, StoredMemory]]:
    scored: list[tuple[float, StoredMemory]] = []
    for mem in mems:
        if mem.superseded:
            continue
        if exclude_id is not None and mem.id == exclude_id:
            continue
        if domain is not None and mem.domain != domain:
            continue
        mem_vec = mem.embedding if mem.embedding is not None else embedder.embed(mem.content)
        sim = cosine(query_vec, mem_vec)
        if sim >= min_cosine:
            scored.append((sim, mem))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:k]


class RuleChannel:
    """CONSTRAINT memories — force-recalled by domain."""

    async def search(
        self, query: str, domain: str | None, ctx: RecallContext
    ) -> list[RecalledItem]:
        items: list[RecalledItem] = []
        if ctx.static_constraints:
            block = "[CONSTRAINTS]\n" + "\n".join(ctx.static_constraints)
            items.append(RecalledItem(block, "rule_force", ctx.counter.count(block)))
        for c in ctx.constraints:
            if c.superseded:
                continue
            items.append(
                RecalledItem(
                    f"[CONSTRAINT] {c.content}",
                    "rule_force",
                    ctx.counter.count(c.content),
                    source_mem=c,
                )
            )
        return items


class ExactChannel:
    """PREFERENCE / EPISODIC memories — keyword match."""

    async def search(
        self, query: str, domain: str | None, ctx: RecallContext
    ) -> list[RecalledItem]:
        keywords = _keywords(query)
        if not keywords:
            return []
        items: list[RecalledItem] = []
        for mem in ctx.documents:
            if mem.superseded:
                continue
            if any(kw in mem.content.lower() for kw in keywords):
                tag = "PREFERENCE" if mem.memory_type == MemoryType.PREFERENCE else "EPISODE"
                text = f"[{tag}] {mem.content[:_SNIPPET_MAX_CHARS]}"
                items.append(RecalledItem(text, "exact", ctx.counter.count(text), source_mem=mem))
        return items


class SemanticChannel:
    """PREFERENCE / EPISODIC memories — embedding similarity."""

    _TOP_K = 8
    # HashEmbedder cosine is conservative; raise to ~0.4-0.5 with a real embedder.
    _MIN_COSINE = 0.3

    def __init__(self, embedder: HashEmbedder) -> None:
        self._embedder = embedder

    async def search(
        self, query: str, domain: str | None, ctx: RecallContext
    ) -> list[RecalledItem]:
        if not query.strip():
            return []
        query_vec = self._embedder.embed(query)
        scored = top_k_by_cosine(
            query_vec,
            ctx.documents,
            self._embedder,
            k=self._TOP_K,
            min_cosine=self._MIN_COSINE,
            domain=domain,
        )
        items: list[RecalledItem] = []
        for _sim, mem in scored:
            tag = "PREFERENCE" if mem.memory_type == MemoryType.PREFERENCE else "EPISODE"
            text = f"[{tag}] {mem.content[:_SNIPPET_MAX_CHARS]}"
            items.append(RecalledItem(text, "semantic", ctx.counter.count(text), source_mem=mem))
        return items


class EntityChannel:
    """FACT memories — returned by entity_id with version."""

    async def search(
        self, query: str, domain: str | None, ctx: RecallContext
    ) -> list[RecalledItem]:
        items: list[RecalledItem] = []
        for mem in ctx.facts:
            text = f"[FACT:{mem.entity_id} v={mem.version}] {mem.content}"
            items.append(RecalledItem(text, "entity", ctx.counter.count(text), source_mem=mem))
        return items
