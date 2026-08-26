from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.base.time import now_utc
from prodagent.cognition.context.budget import BudgetTracker, TokenCounter
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
from prodagent.cognition.memory.facts import FactStore
from prodagent.cognition.memory.forgetting import RECALL_FLOOR, activation
from prodagent.cognition.memory.storage import MemoryRecord, MemoryType
from prodagent.cognition.memory.touch_worker import TouchBackWorker
from prodagent.kernel.bus import Gate, HookEvent
from prodagent.kernel.state import is_child_subordinate
from prodagent.kernel.types import RunState

if TYPE_CHECKING:
    from pathlib import Path

    from prodagent.base.config import FrameworkConfig
    from prodagent.cognition.memory.classification import MemoryClassifier
    from prodagent.kernel.state import AgentRun
    from prodagent.ports.document import DocumentStore
    from prodagent.ports.graph import GraphStore
    from prodagent.ports.llm import LLMClient

logger = logging.getLogger(__name__)

_DEFAULT_BUDGET = 4_000

__all__ = ["MemoryManager", "MemoryProvider", "build_memory_manager"]


@runtime_checkable
class MemoryProvider(Protocol):
    """``MemoryManager`` satisfies this structurally — lets ``Agent`` locate
    the manager via ``isinstance`` without depending on its concrete class."""

    async def recall(self, *args: Any, **kwargs: Any) -> Any: ...

    async def classify(self, *args: Any, **kwargs: Any) -> Any: ...


class MemoryManager:
    """Orchestrator over a :class:`DocumentStore` + :class:`GraphStore`."""

    _documents: DocumentStore
    _facts: FactStore

    def __init__(
        self,
        documents: DocumentStore,
        facts: GraphStore,
        *,
        constraints: list[str] | None = None,
        embedder: HashEmbedder | None = None,
        channels: list[Any] | None = None,
        classifier: MemoryClassifier | None = None,
        conflict_pipeline: ConflictPipeline | None = None,
        budget: int = _DEFAULT_BUDGET,
    ) -> None:
        self._documents = documents
        self._facts = FactStore(facts)
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
        self._conflict_pipeline = conflict_pipeline or ConflictPipeline(
            EmbeddingCandidateFilter(self._embedder),
            None,
            SupersedeAction(documents),
        )

        self._touch_worker = TouchBackWorker(documents)

    def attach_hooks(self, hooks: Any) -> None:
        self._hooks = hooks

    def _get_write_lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    async def recall(self, query: str, domain: str | None = None) -> str:
        now = now_utc()
        async with self._get_write_lock():
            constraints = await self._documents.load_constraints()
            documents = await self._documents.load_memories()
            facts = await self._facts.load_all()
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
        budget = BudgetTracker(self._budget)
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
                if not budget.try_take(item.token_count):
                    continue
                recalled.append(item.content)
                if stored is not None and stored.id:
                    seen_ids.add(stored.id)
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
        record_types = [r.memory_type.value for r in records]
        contents = [r.content for r in records]
        if self._hooks is not None:
            await self._hooks.fire(
                HookEvent.MEMORY_CLASSIFY,
                scanned=scanned,
                written=len(records),
                types=types,
                record_types=record_types,
                contents=contents,
                run_id=getattr(run, "run_id", ""),
            )

    async def _write_memory(self, record: MemoryRecord) -> None:
        await self._persist(record, run_conflict=True)

    async def _persist(self, record: MemoryRecord, *, run_conflict: bool) -> None:
        async with self._get_write_lock():
            if self._hooks is not None and record.content:
                await self._hooks.check_blocking(
                    Gate.DOCUMENT_ADD,
                    document=record.content,
                    source=record.source or "classifier",
                )

            if record.embedding is None and record.content:
                record.embedding = self._embedder.embed(record.content)

            if record.memory_type is MemoryType.FACT:
                await self._facts.write(record)
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
                    for m in await self._documents.load_memories()
                    if m.memory_type == record.memory_type
                ]
                if not any(m.content[:80] == prefix for m in existing if not m.superseded):
                    await self._documents.append_soft(record)

    async def add_memory(self, record: MemoryRecord) -> None:
        await self._persist(record, run_conflict=False)


def _resolve_documents(
    documents: DocumentStore | None, framework_config: FrameworkConfig | None
) -> DocumentStore:
    if documents is not None:
        return documents
    if framework_config is None:
        raise ValueError("MemoryManager requires either explicit documents or a framework_config")
    from prodagent.backends.factory import resolve_document

    return resolve_document(framework_config)


def _resolve_facts(
    facts: GraphStore | None, framework_config: FrameworkConfig | None
) -> GraphStore:
    if facts is not None:
        return facts
    if framework_config is None:
        raise ValueError("MemoryManager requires either explicit facts or a framework_config")
    from prodagent.backends.factory import resolve_graph

    return resolve_graph(framework_config)


def _resolve_classifier(
    classifier: MemoryClassifier | None, framework_config: FrameworkConfig | None
) -> MemoryClassifier | None:
    if classifier is not None or framework_config is None:
        return classifier
    from prodagent.backends.factory import resolve_llm
    from prodagent.cognition.memory.classification import MemoryClassifier

    return MemoryClassifier(resolve_llm(framework_config))


def _resolve_conflict_policy(
    conflict_policy: DefaultConflictPolicy | None, framework_config: FrameworkConfig | None
) -> DefaultConflictPolicy | None:
    if conflict_policy is not None or framework_config is None:
        return conflict_policy
    from prodagent.backends.factory import resolve_llm

    return DefaultConflictPolicy(llm_client=resolve_llm(framework_config))


def build_memory_manager(
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
    aux_llm: LLMClient | None = None,
    memory_dir: str | Path | None = None,
    clean: bool = False,
) -> MemoryManager:
    """Assemble a :class:`MemoryManager`.

    Convenience kwargs absorb the wiring every app repeats: ``aux_llm`` fills
    the classifier + conflict policy when they're not given; ``memory_dir``
    routes framework-resolved file stores into a caller-owned directory
    (and, with ``clean=True``, wipes it first) so demos start reproducible.
    """
    if memory_dir is not None:
        import shutil
        from dataclasses import replace as _dc_replace
        from pathlib import Path as _Path

        if clean:
            shutil.rmtree(memory_dir, ignore_errors=True)
        _Path(memory_dir).mkdir(parents=True, exist_ok=True)
        if framework_config is None:
            from prodagent.base.config import FrameworkConfig as _FW

            framework_config = _FW.default()
        framework_config = _dc_replace(
            framework_config,
            orchestration=_dc_replace(framework_config.orchestration, runs_dir=str(memory_dir)),
        )
    if aux_llm is not None:
        if classifier is None:
            from prodagent.cognition.memory.classification import MemoryClassifier

            classifier = MemoryClassifier(aux_llm)
        if conflict_policy is None:
            conflict_policy = DefaultConflictPolicy(llm_client=aux_llm)

    resolved_documents = _resolve_documents(documents, framework_config)
    resolved_facts = _resolve_facts(facts, framework_config)
    resolved_classifier = _resolve_classifier(classifier, framework_config)
    resolved_conflict_policy = _resolve_conflict_policy(conflict_policy, framework_config)
    resolved_embedder = embedder or HashEmbedder()

    conflict_pipeline = ConflictPipeline(
        candidate_filter or EmbeddingCandidateFilter(resolved_embedder),
        resolved_conflict_policy,
        SupersedeAction(resolved_documents),
    )

    return MemoryManager(
        resolved_documents,
        resolved_facts,
        constraints=constraints,
        embedder=resolved_embedder,
        channels=channels,
        classifier=resolved_classifier,
        conflict_pipeline=conflict_pipeline,
        budget=budget,
    )
