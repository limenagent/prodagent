from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.cognition.context.budget import TokenCounter
from prodagent.cognition.memory.channels import (
    DEFAULT_MERGE_ORDER,
    EntityChannel,
    ExactChannel,
    RecallContext,
    RecalledItem,
    RuleChannel,
    SemanticChannel,
)
from prodagent.cognition.memory.classification import reasoning_texts
from prodagent.cognition.memory.conflict import (
    ConflictPipeline,
    DefaultConflictPolicy,
    EmbeddingCandidateFilter,
    SupersedeAction,
)
from prodagent.cognition.memory.embedder import HashEmbedder
from prodagent.cognition.memory.forgetting import RECALL_FLOOR, activation
from prodagent.cognition.memory.storage import (
    MemoryRecord,
    MemoryType,
    StoredMemory,
    mem_id,
)
from prodagent.cognition.memory.touch_worker import TouchBackWorker
from prodagent.core.state.run import is_child_subordinate
from prodagent.core.time import now_timestamp, now_utc
from prodagent.core.types import RunState
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.events import HookEvent

if TYPE_CHECKING:
    from prodagent.cognition.memory.classification import MemoryClassifier
    from prodagent.core.config import FrameworkConfig
    from prodagent.core.state.run import AgentRun
    from prodagent.ports.document import DocumentStore
    from prodagent.ports.graph import GraphStore

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET = 4_000

__all__ = ["MemoryManager", "MemoryProvider"]


@runtime_checkable
class MemoryProvider(Protocol):
    """``MemoryManager`` satisfies this structurally — lets ``Agent`` locate
    the manager via ``isinstance`` without depending on its concrete class."""

    async def recall(self, *args: Any, **kwargs: Any) -> Any: ...

    async def classify(self, *args: Any, **kwargs: Any) -> Any: ...


class MemoryManager:
    """Orchestrator over a :class:`DocumentStore` + :class:`GraphStore`."""

    _documents: DocumentStore
    _facts: GraphStore

    def __init__(
        self,
        documents: DocumentStore | None = None,
        facts: GraphStore | None = None,
        *,
        framework_config: FrameworkConfig | None = None,
        constraints: list[str] | None = None,
        embedder: HashEmbedder | None = None,
        channels: list[Any] | None = None,
        classifier: MemoryClassifier | None = None,
        candidate_filter: EmbeddingCandidateFilter | None = None,
        conflict_policy: DefaultConflictPolicy | None = None,
        budget: int = _DEFAULT_BUDGET,
    ) -> None:
        if documents is None:
            if framework_config is None:
                raise ValueError(
                    "MemoryManager requires either explicit documents or a framework_config"
                )
            from prodagent.backends.factory import resolve_document

            documents = resolve_document(framework_config)
        if facts is None:
            if framework_config is None:
                raise ValueError(
                    "MemoryManager requires either explicit facts or a framework_config"
                )
            from prodagent.backends.factory import resolve_graph

            facts = resolve_graph(framework_config)
        # Aux LLM drives classify + conflict — lazy-resolved from framework_config.
        if classifier is None and framework_config is not None:
            from prodagent.backends.factory import resolve_llm
            from prodagent.cognition.memory.classification import MemoryClassifier

            classifier = MemoryClassifier(resolve_llm(framework_config))
        if conflict_policy is None and framework_config is not None:
            from prodagent.backends.factory import resolve_llm

            conflict_policy = DefaultConflictPolicy(llm_client=resolve_llm(framework_config))
        self._documents = documents
        self._facts = facts
        self._static_constraints: list[str] = list(constraints or [])
        self._budget = budget
        self._hooks: Any | None = None
        self._write_lock: asyncio.Lock | None = None
        self._counter = TokenCounter()

        self._embedder: HashEmbedder = embedder or HashEmbedder()

        self._channels: list[Any] = channels or [
            RuleChannel(),
            EntityChannel(),
            ExactChannel(),
            SemanticChannel(self._embedder),
        ]
        self._merge_order = DEFAULT_MERGE_ORDER

        self._classifier: MemoryClassifier | None = classifier
        self._candidate_filter = candidate_filter or EmbeddingCandidateFilter(self._embedder)
        self._conflict_pipeline = ConflictPipeline(
            self._candidate_filter,
            conflict_policy,
            SupersedeAction(documents),
        )

        self._touch_worker = TouchBackWorker(documents)

    def attach_hooks(self, hooks: Any) -> None:
        self._hooks = hooks

    def _get_write_lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    def _write_fact(self, record: MemoryRecord) -> None:
        # Each fact is one graph node (label ``Fact``); re-writing the same
        # entity_id merges properties so the latest content/version wins.
        # Edges between entities are the caller's job.
        eid = record.entity_id or mem_id(record.content)
        existing = self._facts.get_node(eid)
        version = (existing["properties"].get("version", 0) + 1) if existing else 1
        self._facts.add_node(
            eid,
            labels=["Fact"],
            properties={
                "content": record.content,
                "entity_id": eid,
                "domain": record.domain,
                "source": record.source,
                "version": version,
                "created_at": now_timestamp(),
                "embedding": record.embedding,
            },
        )

    def _load_facts(self) -> list[StoredMemory]:
        out: list[StoredMemory] = []
        for node in self._facts.list_nodes(label="Fact"):
            p = node["properties"]
            out.append(
                StoredMemory(
                    id=node["id"],
                    content=p.get("content", ""),
                    memory_type=MemoryType.FACT,
                    domain=p.get("domain", "general"),
                    entity_id=node["id"],
                    created_at=p.get("created_at", ""),
                    version=p.get("version", 1),
                    embedding=p.get("embedding"),
                )
            )
        return out

    async def recall(self, query: str, domain: str | None = None) -> str:
        now = now_utc()
        async with self._get_write_lock():
            constraints = self._documents.load_constraints()
            documents = self._documents.load_memories()
            facts = self._load_facts()
        ctx = RecallContext(
            constraints=constraints,
            documents=documents,
            facts=facts,
            static_constraints=self._static_constraints,
            counter=self._counter,
        )

        results = await asyncio.gather(*(ch.search(query, domain, ctx) for ch in self._channels))
        by_stage: dict[str, list[RecalledItem]] = {}
        for items in results:
            for item in items:
                by_stage.setdefault(item.recall_stage, []).append(item)

        blocks: list[str] = []
        budget_left = self._budget
        recalled: list[str] = []
        seen_ids: set[str] = set()

        for stage in self._merge_order:
            for item in by_stage.get(stage, []):
                stored = item.source_mem
                if stored is not None:
                    if stored.superseded:
                        continue
                    if stored.id in seen_ids:
                        continue
                    activation_value = activation(stored, now)
                    if activation_value < RECALL_FLOOR:
                        continue
                    if activation_value < 1.0:
                        item = RecalledItem(
                            content=f"{item.content} [conf={activation_value:.1f}]",
                            recall_stage=item.recall_stage,
                            token_count=item.token_count,
                            source_mem=stored,
                        )
                if budget_left - item.token_count < 0:
                    continue
                recalled.append(item.content)
                budget_left -= item.token_count
                if stored is not None and stored.id:
                    seen_ids.add(stored.id)
                # Touch-back reinforces retrieval for soft memories only —
                # constraints are force-recalled, facts use versioning.
                if (
                    stored is not None
                    and stored.id
                    and stored.memory_type in (MemoryType.PREFERENCE, MemoryType.EPISODIC)
                ):
                    self._touch_worker.enqueue(stored.id)

        if recalled:
            blocks.append("[RECALLED]\n" + "\n".join(recalled))

        logger.debug(
            "[memory] recall: %s for %.60r",
            {stage: len(items) for stage, items in by_stage.items()},
            query,
        )
        return "\n\n".join(blocks) if blocks else ""

    async def aclose(self) -> None:
        await self._touch_worker.aclose()

    async def classify(self, *, run: AgentRun | None = None, state: str = "", **_: Any) -> None:
        if state != RunState.COMPLETED or run is None:
            return
        if self._classifier is None:
            logger.info("Memory.classify: skipped (no classifier attached)")
            return
        if is_child_subordinate(run):
            return

        texts = reasoning_texts(run)
        if not texts:
            return

        records: list[MemoryRecord] = []
        for text in texts[-3:]:
            try:
                record = await self._classifier.classify(text)
            except Exception as exc:
                logger.error("[memory] classifier error: %s", exc)
                continue
            if record is not None:
                records.append(record)

        for record in records:
            await self._write_memory(record)

        scanned = min(3, len(texts))
        types = ", ".join(sorted({r.memory_type.value for r in records})) if records else ""
        if self._hooks is not None:
            await self._hooks.fire(
                HookEvent.MEMORY_CLASSIFY,
                scanned=scanned,
                written=len(records),
                types=types,
                run_id=getattr(run, "run_id", ""),
            )

    async def _write_memory(self, record: MemoryRecord) -> None:
        await self._persist(record, run_conflict=True)

    async def _persist(self, record: MemoryRecord, *, run_conflict: bool) -> None:
        async with self._get_write_lock():
            if self._hooks is not None and record.content:
                await self._hooks.check_blocking(
                    CheckPoint.DOCUMENT_ADD,
                    document=record.content,
                    source=record.source or "classifier",
                )

            if record.embedding is None and record.content:
                record.embedding = self._embedder.embed(record.content)

            if record.memory_type is MemoryType.FACT:
                self._write_fact(record)
                return

            discarded = (
                await self._conflict_pipeline.resolve(record, self._documents)
                if run_conflict
                else False
            )
            if not discarded:
                prefix = record.content[:80]
                existing = [
                    m
                    for m in self._documents.load_memories()
                    if m.memory_type == record.memory_type
                ]
                if not any(m.content[:80] == prefix for m in existing if not m.superseded):
                    self._documents.append_soft(record)

    async def add_memory(self, record: MemoryRecord) -> None:
        await self._persist(record, run_conflict=False)
