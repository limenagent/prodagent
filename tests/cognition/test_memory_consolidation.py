from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from prodagent.backends.file import FileDocumentStore
from prodagent.backends.memory import InMemoryGraphStore
from prodagent.cognition.memory.classification import MemoryClassifier
from prodagent.cognition.memory.conflict import DefaultConflictPolicy, EmbeddingCandidateFilter
from prodagent.cognition.memory.forgetting import activation
from prodagent.cognition.memory.manager import MemoryManager, build_memory_manager
from prodagent.cognition.memory.storage import (
    MemoryRecord,
    MemoryType,
    StoredMemory,
)
from prodagent.core.state.run import AgentRun
from prodagent.core.types import RunState

if TYPE_CHECKING:
    from pathlib import Path


def _reactive_run(*texts: str, completed: bool = True) -> AgentRun:
    run = AgentRun(run_id="test", task="t")
    run.state = RunState.COMPLETED if completed else RunState.FAILED
    for text in texts:
        run.messages.append({"role": "assistant", "content": text})
    return run


def test_memory_record_ttl_days_default():
    mem = MemoryRecord(content="hello", memory_type=MemoryType.PREFERENCE, domain="general")
    assert mem.ttl_days is None


def test_memory_record_ttl_days_explicit():
    mem = MemoryRecord(
        content="last week outage",
        memory_type=MemoryType.EPISODIC,
        domain="reliability",
        ttl_days=14,
    )
    assert mem.ttl_days == 14


def test_stored_memory_to_dict_round_trip():
    stored = StoredMemory(
        id="x",
        content="test",
        memory_type=MemoryType.EPISODIC,
        domain="ops",
        ttl_days=7,
    )
    d = stored.to_dict()
    assert d["ttl_days"] == 7
    assert d["memory_type"] == "episodic"
    assert d["superseded"] is False
    assert d["version"] == 1
    assert "weight" not in d


def test_memory_record_accepts_plain_string_type():
    mem = MemoryRecord(content="x", memory_type="fact")
    assert mem.memory_type is MemoryType.FACT


def test_memory_record_has_no_stateful_fields():
    mem = MemoryRecord(content="x", memory_type=MemoryType.PREFERENCE)
    assert not hasattr(mem, "weight")
    assert not hasattr(mem, "id")
    assert not hasattr(mem, "created_at")
    assert not hasattr(mem, "superseded")
    assert not hasattr(mem, "access_count")


def test_stored_memory_from_record_stamps_id_and_created_at():
    record = MemoryRecord(content="hello", memory_type=MemoryType.PREFERENCE, domain="x")
    stored = StoredMemory.from_record(record, id="abc", created_at="2026-01-01T00:00:00")
    assert stored.id == "abc"
    assert stored.created_at == "2026-01-01T00:00:00"
    assert stored.superseded is False
    assert stored.version == 1
    assert stored.content == "hello"


class _FakeLLM:
    def __init__(self, json_body: str) -> None:
        self._body = json_body

    async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
        class R:
            content = None

        R.content = self._body
        return R()


@pytest.mark.asyncio
async def test_consolidator_constraint_classification():
    llm = _FakeLLM(
        '{"memory_type":"constraint","domain":"database","force_recall":true,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    mem = await MemoryClassifier(llm_client=llm).classify("禁止使用ORM")
    assert mem.memory_type is MemoryType.CONSTRAINT
    assert mem.domain == "database"


@pytest.mark.asyncio
async def test_consolidator_fact_classification():
    llm = _FakeLLM(
        '{"memory_type":"fact","domain":"k8s","force_recall":false,'
        '"confidence":0.85,"ttl_days":null,"entity_id":"pod:abc","trigger_conditions":[]}'
    )
    mem = await MemoryClassifier(llm_client=llm).classify("pod abc is running v2.1")
    assert mem.memory_type is MemoryType.FACT
    assert mem.entity_id == "pod:abc"
    assert mem.domain == "k8s"


@pytest.mark.asyncio
async def test_consolidator_episodic_ttl_propagated():
    llm = _FakeLLM(
        '{"memory_type":"episodic","domain":"ops","force_recall":false,'
        '"confidence":0.8,"ttl_days":30,"entity_id":"","trigger_conditions":[]}'
    )
    mem = await MemoryClassifier(llm_client=llm).classify("outage 30 days ago")
    assert mem.ttl_days == 30
    assert mem.memory_type is MemoryType.EPISODIC


@pytest.mark.asyncio
async def test_consolidator_json_parse_failure_falls_back_to_episodic():
    class BadLLM:
        async def complete(self, *a, **kw):
            class R:
                content = "not json {{"

            return R()

    mem = await MemoryClassifier(llm_client=BadLLM(), max_retries=0).classify("x")
    assert mem.memory_type is MemoryType.EPISODIC


@pytest.mark.asyncio
async def test_consolidator_none_type_returns_none():
    llm = _FakeLLM('{"memory_type":"none","content":"","domain":"general"}')
    mem = await MemoryClassifier(llm_client=llm).classify("status ack text")
    assert mem is None


@pytest.mark.asyncio
async def test_consolidator_extracts_distilled_content():
    llm = _FakeLLM(
        '{"memory_type":"fact","content":"PR #4412 removed buffer-pool in ProcessBatch()",'
        '"domain":"payments","entity_id":"pr:4412","ttl_days":null}'
    )
    mem = await MemoryClassifier(llm_client=llm).classify(
        "All three sub-agents have returned. SLO triage check is complete. "
        "The root cause is PR #4412 which removed the buffer-pool pattern."
    )
    assert mem is not None
    assert mem.content == "PR #4412 removed buffer-pool in ProcessBatch()"
    assert mem.entity_id == "pr:4412"


def make_store(tmp: Path, **kw) -> MemoryManager:
    docs = FileDocumentStore(tmp)
    facts = InMemoryGraphStore()
    return build_memory_manager(docs, facts, **kw)


def _make_store_with_llm(tmp: Path, llm: _FakeLLM, **kw) -> MemoryManager:
    return make_store(tmp, classifier=MemoryClassifier(llm), **kw)


def _low_threshold_filter(manager: MemoryManager) -> EmbeddingCandidateFilter:
    return EmbeddingCandidateFilter(manager._embedder, min_cosine=0.0)


def _soft_mem(
    *,
    id: str,
    content: str,
    memory_type: str = "preference",
    domain: str = "general",
    ttl_days: int | None = None,
    created_at: str = "2026-01-01T00:00:00",
    superseded: bool = False,
    access_count: int = 0,
    last_access: str = "",
) -> dict:
    return {
        "id": id,
        "content": content,
        "memory_type": memory_type,
        "domain": domain,
        "entity_id": "",
        "ttl_days": ttl_days,
        "force_recall": False,
        "created_at": created_at,
        "superseded": superseded,
        "version": 1,
        "source": "",
        "access_count": access_count,
        "last_access": last_access,
    }


def _fact_mem(*, entity_id: str, content: str, version: int = 1) -> dict:
    """Properties for a Fact graph node (mirrors what ``_write_fact`` stores)."""
    return {
        "content": content,
        "entity_id": entity_id,
        "domain": "general",
        "source": "",
        "version": version,
        "created_at": "2026-01-01T00:00:00",
        "embedding": None,
    }


async def _seed_fact(store: MemoryManager, **kw) -> None:
    """Pre-seed a FACT node directly into the graph (bypasses the manager)."""
    props = _fact_mem(**kw)
    await store._facts.add_node(props["entity_id"], labels=["Fact"], properties=props)


@pytest.mark.asyncio
async def test_recall_empty_store_returns_empty_string(tmp_path):
    store = make_store(tmp_path)
    assert await store.recall("anything") == ""


@pytest.mark.asyncio
async def test_recall_static_constraints_always_returned(tmp_path):
    store = make_store(tmp_path, constraints=["禁止ORM", "必须审批"])
    result = await store.recall("some query")
    assert "禁止ORM" in result
    assert "必须审批" in result
    assert "[CONSTRAINTS]" in result


@pytest.mark.asyncio
async def test_recall_keyword_match_on_soft_memories(tmp_path):
    store = make_store(tmp_path)
    store._documents._memories_file.write_text(
        json.dumps([_soft_mem(id="abc", content="用户喜欢Python风格")])
    )

    result = await store.recall("python style code")
    assert "用户喜欢Python风格" in result

    result2 = await store.recall("network latency")
    assert "用户喜欢Python风格" not in result2


@pytest.mark.asyncio
async def test_recall_constraint_from_file_force_recalled(tmp_path):
    store = make_store(tmp_path)
    store._documents._memories_file.write_text(
        json.dumps([_soft_mem(id="c1", content="禁止原生SQL以外的ORM", memory_type="constraint")])
    )

    result = await store.recall("write database query")
    assert "禁止原生SQL以外的ORM" in result
    assert "[CONSTRAINT]" in result


@pytest.mark.asyncio
async def test_recall_superseded_memory_excluded(tmp_path):
    store = make_store(tmp_path)
    memories = [
        _soft_mem(id="active", content="active python pref"),
        _soft_mem(id="stale", content="stale python pref", superseded=True),
    ]
    store._documents._memories_file.write_text(json.dumps(memories))

    result = await store.recall("python")
    assert "active python pref" in result
    assert "stale python pref" not in result


@pytest.mark.asyncio
async def test_recall_budget_capped(tmp_path):
    store = make_store(tmp_path, budget=50)
    memories = [_soft_mem(id=f"m{i}", content=f"keyword memory item {i} " * 10) for i in range(20)]
    store._documents._memories_file.write_text(json.dumps(memories))
    result = await store.recall("keyword memory")
    count = result.count("[PREFERENCE]")
    assert count < 20


@pytest.mark.asyncio
async def test_recall_runs_all_four_channels(tmp_path):

    store = make_store(
        tmp_path,
        constraints=["禁止ORM"],
    )
    await _seed_fact(store, entity_id="user_plan", content="套餐是基础版", version=2)
    store._documents._memories_file.write_text(
        json.dumps([_soft_mem(id="p1", content="用户喜欢Python风格")])
    )

    result = await store.recall("python style")
    assert "[CONSTRAINT]" in result or "[CONSTRAINTS]" in result
    assert "[FACT:user_plan v=2]" in result
    assert "[PREFERENCE]" in result
    assert "用户喜欢Python风格" in result


@pytest.mark.asyncio
async def test_recall_priority_rule_before_semantic(tmp_path):
    store = make_store(tmp_path, constraints=["禁止ORM"], budget=30)
    memories = [
        _soft_mem(id="p1", content="python keyword preference " * 10),
    ]
    store._documents._memories_file.write_text(json.dumps(memories))

    result = await store.recall("python keyword")
    assert "禁止ORM" in result


@pytest.mark.asyncio
async def test_recall_fact_carries_version_number(tmp_path):
    store = make_store(tmp_path)
    await _seed_fact(store, entity_id="pod:payment", content="payment pod v2.15", version=3)

    result = await store.recall("anything")
    assert "[FACT:pod:payment v=3]" in result


@pytest.mark.asyncio
async def test_recall_domain_param_is_accepted(tmp_path):
    store = make_store(tmp_path, constraints=["禁止ORM"])
    result = await store.recall("query", domain=None)
    assert "禁止ORM" in result


@pytest.mark.asyncio
async def test_recall_includes_confidence_marker_for_decaying_memory(tmp_path):
    store = make_store(tmp_path)
    created = (datetime.now(UTC) - timedelta(days=6)).isoformat()
    store._documents._memories_file.write_text(
        json.dumps(
            [
                _soft_mem(
                    id="decaying",
                    content="decaying python event",
                    memory_type="episodic",
                    ttl_days=7,
                    created_at=created,
                )
            ]
        )
    )

    result = await store.recall("python")
    assert "decaying python event" in result
    assert "[conf=" in result


@pytest.mark.asyncio
async def test_recall_excludes_below_floor_items(tmp_path):
    store = make_store(tmp_path)
    created = (datetime.now(UTC) - timedelta(days=25)).isoformat()
    store._documents._memories_file.write_text(
        json.dumps(
            [
                _soft_mem(
                    id="archived",
                    content="archived python event",
                    memory_type="episodic",
                    ttl_days=7,
                    created_at=created,
                )
            ]
        )
    )

    result = await store.recall("python")
    assert "archived python event" not in result


@pytest.mark.asyncio
async def test_recall_preference_has_no_confidence_marker(tmp_path):
    store = make_store(tmp_path)
    store._documents._memories_file.write_text(
        json.dumps([_soft_mem(id="pref", content="fresh python preference")])
    )

    result = await store.recall("python")
    assert "fresh python preference" in result
    assert "[conf=" not in result


@pytest.mark.asyncio
async def test_classify_skips_non_completed(tmp_path):
    store = _make_store_with_llm(tmp_path, _FakeLLM("{}"))
    await store.classify(run=_reactive_run("x" * 200, completed=False), state="failed")
    assert not store._documents._memories_file.exists()


@pytest.mark.asyncio
async def test_classify_without_classifier_is_noop(tmp_path):
    store = make_store(tmp_path)
    await store.classify(run=_reactive_run("x" * 200), state="completed")
    assert not store._documents._memories_file.exists()


@pytest.mark.asyncio
async def test_classify_constraint_written_to_file(tmp_path):
    llm = _FakeLLM(
        '{"memory_type":"constraint","domain":"database","force_recall":true,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    store = _make_store_with_llm(tmp_path, llm)
    await store.classify(run=_reactive_run("禁止ORM，必须原生SQL" * 10), state="completed")
    data = json.loads(store._documents._memories_file.read_text())
    assert len(data) == 1
    assert data[0]["memory_type"] == "constraint"
    assert "禁止ORM" in data[0]["content"]


@pytest.mark.asyncio
async def test_classify_fact_upserted_by_entity_id(tmp_path):
    fact_json = (
        '{"memory_type":"fact","domain":"k8s","force_recall":false,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"pod:payment","trigger_conditions":[]}'
    )
    store = _make_store_with_llm(tmp_path, _FakeLLM(fact_json))
    await store.classify(run=_reactive_run("payment pod is running v2.14" * 5), state="completed")
    store._classifier = MemoryClassifier(_FakeLLM(fact_json))
    await store.classify(run=_reactive_run("payment pod now v2.15" * 5), state="completed")
    node = await store._facts.get_node("pod:payment")
    assert node is not None
    assert node["properties"]["version"] == 2
    assert node["properties"]["content"] == "payment pod now v2.15" * 5


@pytest.mark.asyncio
async def test_fact_version_increments_on_repeated_upsert(tmp_path):
    fact_json = (
        '{"memory_type":"fact","domain":"billing","force_recall":false,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"user_plan","trigger_conditions":[]}'
    )
    store = _make_store_with_llm(tmp_path, _FakeLLM(fact_json))
    for content in ["VIP", "基础版", "尊享版"]:
        await store.classify(run=_reactive_run(content * 30), state="completed")
    node = await store._facts.get_node("user_plan")
    assert node is not None
    assert node["properties"]["version"] == 3
    assert node["properties"]["content"] == "尊享版" * 30


@pytest.mark.asyncio
async def test_classify_episodic_gets_default_ttl(tmp_path):
    llm = _FakeLLM(
        '{"memory_type":"episodic","domain":"ops","force_recall":false,'
        '"confidence":0.8,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    store = _make_store_with_llm(tmp_path, llm)
    await store.classify(run=_reactive_run("上周发生网络故障" * 12), state="completed")
    memories = json.loads(store._documents._memories_file.read_text())
    assert any(m.get("ttl_days") == 7 for m in memories)


@pytest.mark.asyncio
async def test_classify_supersedes_conflicting_old_on_write(tmp_path):
    old_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    import hashlib

    new_content = "user downgraded to basic plan for cost reasons" * 5
    transient_id = "transient:" + hashlib.blake2b(new_content.encode(), digest_size=6).hexdigest()

    conflict_llm = _FakeLLM(
        '{"conflicts": [{"winner_id": "' + transient_id + '", '
        '"loser_id": "old_pref", "reason": "superseded_by"}]}'
    )
    new_pref_json = (
        '{"memory_type":"preference","domain":"billing","force_recall":false,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    store = make_store(
        tmp_path,
        classifier=MemoryClassifier(_FakeLLM(new_pref_json)),
        conflict_policy=DefaultConflictPolicy(llm_client=conflict_llm),
        candidate_filter=_low_threshold_filter(make_store(tmp_path)),
    )
    store._documents._memories_file.write_text(
        json.dumps(
            [
                _soft_mem(
                    id="old_pref",
                    content="user likes the VIP plan",
                    domain="billing",
                    created_at=old_date,
                )
            ]
        )
    )

    await store.classify(run=_reactive_run(new_content), state="completed")

    memories = json.loads(store._documents._memories_file.read_text())
    old = next(m for m in memories if m["id"] == "old_pref")
    assert old["superseded"] is True


@pytest.mark.asyncio
async def test_classify_constraint_dedup_via_pipeline(tmp_path):
    old_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    import hashlib

    new_content = "rollback must be approved by the operator before executing" * 5
    transient_id = "transient:" + hashlib.blake2b(new_content.encode(), digest_size=6).hexdigest()

    conflict_llm = _FakeLLM(
        '{"conflicts": [{"winner_id": "' + transient_id + '", '
        '"loser_id": "old_rule", "reason": "duplicate"}]}'
    )
    new_constraint_json = (
        '{"memory_type":"constraint","domain":"ops","force_recall":false,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    store = make_store(
        tmp_path,
        classifier=MemoryClassifier(_FakeLLM(new_constraint_json)),
        conflict_policy=DefaultConflictPolicy(llm_client=conflict_llm),
        candidate_filter=_low_threshold_filter(make_store(tmp_path)),
    )
    store._documents._memories_file.write_text(
        json.dumps(
            [
                _soft_mem(
                    id="old_rule",
                    content="code-change rollback requires explicit operator approval",
                    memory_type="constraint",
                    domain="ops",
                    created_at=old_date,
                )
            ]
        )
    )

    await store.classify(run=_reactive_run(new_content), state="completed")

    memories = json.loads(store._documents._memories_file.read_text())
    old = next(m for m in memories if m["id"] == "old_rule")
    assert old["superseded"] is True


@pytest.mark.asyncio
async def test_classify_discards_new_memory_when_old_wins(tmp_path):
    old_date = (datetime.now(UTC) - timedelta(days=30)).isoformat()

    import hashlib

    new_content = "user downgraded to basic plan for cost reasons" * 5
    transient_id = "transient:" + hashlib.blake2b(new_content.encode(), digest_size=6).hexdigest()

    conflict_llm = _FakeLLM(
        '{"conflicts": [{"winner_id": "old_pref", "loser_id": "' + transient_id + '", '
        '"reason": "duplicate"}]}'
    )
    new_pref_json = (
        '{"memory_type":"preference","domain":"billing","force_recall":false,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    store = make_store(
        tmp_path,
        classifier=MemoryClassifier(_FakeLLM(new_pref_json)),
        conflict_policy=DefaultConflictPolicy(llm_client=conflict_llm),
        candidate_filter=_low_threshold_filter(make_store(tmp_path)),
    )
    store._documents._memories_file.write_text(
        json.dumps(
            [
                _soft_mem(
                    id="old_pref",
                    content="user likes the VIP plan",
                    domain="billing",
                    created_at=old_date,
                )
            ]
        )
    )

    await store.classify(run=_reactive_run(new_content), state="completed")

    memories = json.loads(store._documents._memories_file.read_text())
    old = next(m for m in memories if m["id"] == "old_pref")
    assert old["superseded"] is False
    assert len(memories) == 1
    assert not any(m["id"] == transient_id for m in memories)


@pytest.mark.asyncio
async def test_classify_no_conflict_leaves_old_untouched(tmp_path):
    conflict_llm = _FakeLLM('{"conflicts": []}')
    new_pref_json = (
        '{"memory_type":"preference","domain":"billing","force_recall":false,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    store = make_store(
        tmp_path,
        classifier=MemoryClassifier(_FakeLLM(new_pref_json)),
        conflict_policy=DefaultConflictPolicy(llm_client=conflict_llm),
        candidate_filter=_low_threshold_filter(make_store(tmp_path)),
    )
    store._documents._memories_file.write_text(
        json.dumps([_soft_mem(id="old_pref", content="user likes the VIP plan", domain="billing")])
    )

    await store.classify(
        run=_reactive_run("user downgraded to basic plan for cost reasons" * 5), state="completed"
    )

    memories = json.loads(store._documents._memories_file.read_text())
    old = next(m for m in memories if m["id"] == "old_pref")
    assert old["superseded"] is False


@pytest.mark.asyncio
async def test_classify_no_conflict_policy_writes_without_checking(tmp_path):
    new_pref_json = (
        '{"memory_type":"preference","domain":"general","force_recall":false,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    store = make_store(tmp_path, classifier=MemoryClassifier(_FakeLLM(new_pref_json)))
    await store.classify(run=_reactive_run("user prefers concise answers" * 10), state="completed")
    memories = json.loads(store._documents._memories_file.read_text())
    assert len(memories) == 1
    assert memories[0]["superseded"] is False


@pytest.mark.asyncio
async def test_classify_skips_already_superseded_as_candidate(tmp_path):
    conflict_llm = _FakeLLM('{"conflicts": []}')
    new_pref_json = (
        '{"memory_type":"preference","domain":"billing","force_recall":false,'
        '"confidence":0.9,"ttl_days":null,"entity_id":"","trigger_conditions":[]}'
    )
    store = make_store(
        tmp_path,
        classifier=MemoryClassifier(_FakeLLM(new_pref_json)),
        conflict_policy=DefaultConflictPolicy(llm_client=conflict_llm),
        candidate_filter=_low_threshold_filter(make_store(tmp_path)),
    )
    store._documents._memories_file.write_text(
        json.dumps(
            [
                _soft_mem(id="old", content="likes VIP plan", domain="billing", superseded=True),
            ]
        )
    )

    await store.classify(
        run=_reactive_run("downgraded to basic plan for cost" * 5), state="completed"
    )
    memories = json.loads(store._documents._memories_file.read_text())
    old = next(m for m in memories if m["id"] == "old")
    assert old["superseded"] is True


class TestActivation:
    def setup_method(self):
        self.now = datetime(2026, 7, 8)

    def _activation(
        self,
        *,
        age_days,
        ttl_days=7,
        access_count=0,
        last_access=None,
        memory_type="episodic",
    ):
        created = (self.now - timedelta(days=age_days)).isoformat()
        mem = StoredMemory(
            id="t",
            content="x",
            memory_type=memory_type,
            domain="g",
            ttl_days=ttl_days,
            created_at=created,
            access_count=access_count,
            last_access=last_access or "",
        )
        return activation(mem, self.now)

    def test_fresh_memory_high_activation(self):
        assert self._activation(age_days=2) > 0.5

    def test_decay_band_mid_activation(self):
        act = self._activation(age_days=8)
        assert 0.05 < act < 1.0

    def test_3x_ttl_below_floor(self):
        assert self._activation(age_days=25) < 0.05

    def test_frequently_accessed_resists_decay(self):
        assert self._activation(age_days=25, access_count=3) > 0.05

    def test_recent_access_lifts_activation(self):
        recent_access = (self.now - timedelta(days=3)).isoformat()
        act = self._activation(age_days=25, last_access=recent_access)
        assert act > 0.05

    def test_old_access_does_not_lift(self):
        old_access = (self.now - timedelta(days=10)).isoformat()
        act = self._activation(age_days=25, last_access=old_access)
        assert act < 0.05

    def test_constraint_returns_one(self):
        assert self._activation(age_days=999, memory_type="constraint") == 1.0

    def test_fact_returns_one(self):
        assert self._activation(age_days=999, memory_type="fact") == 1.0

    def test_no_ttl_eternal_preference(self):
        assert self._activation(age_days=999, ttl_days=None, memory_type="preference") == 1.0


@pytest.mark.asyncio
async def test_classify_plan_first_run_uses_messages(tmp_path):
    from prodagent.core.state.run import AgentRun
    from prodagent.core.types import RunState

    captured: list[str] = []

    class _CaptureLLM:
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            captured.append(messages[0]["content"])

            class R:
                content = (
                    '{"memory_type":"episodic","domain":"ops","force_recall":false,'
                    '"confidence":0.8,"ttl_days":7,"entity_id":"","trigger_conditions":[]}'
                )

            return R()

    run = AgentRun(run_id="x", task="fix oom")
    run.state = RunState.COMPLETED
    long_output = "Root cause: OOM kill from unbounded heap growth in payment-service. " + "x" * 20

    run.messages = [
        {"role": "user", "content": "fix oom"},
        {"role": "assistant", "content": long_output},
    ]

    store = make_store(tmp_path, classifier=MemoryClassifier(_CaptureLLM()))
    await store.classify(state="completed", run=run)

    assert len(captured) == 1, "classifier must be called once"
    assert "OOM" in captured[0]


@pytest.mark.asyncio
async def test_custom_classifier_takes_precedence(tmp_path):

    class RuleClassifier:
        async def classify(self, raw):
            return MemoryRecord(content=raw, memory_type=MemoryType.CONSTRAINT, domain="x")

    store = make_store(tmp_path, classifier=RuleClassifier())
    await store.classify(run=_reactive_run("y" * 200), state="completed")
    data = json.loads(store._documents._memories_file.read_text())
    assert len(data) == 1
    assert data[0]["memory_type"] == "constraint"


@pytest.mark.asyncio
async def test_conflict_verdict_age_swaps_inverted_llm_verdict():
    from prodagent.cognition.memory.conflict import DefaultConflictPolicy
    from prodagent.cognition.memory.storage import MemoryRecord, MemoryType, StoredMemory

    old = StoredMemory.from_record(
        MemoryRecord(content="likes VIP benefits", memory_type=MemoryType.PREFERENCE),
        id="old-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    new = StoredMemory.from_record(
        MemoryRecord(content="downgraded to basic plan", memory_type=MemoryType.PREFERENCE),
        id="new-1",
        created_at="2026-07-01T00:00:00+00:00",
    )
    inverted = (
        '{"conflicts": [{"winner_id": "old-1", "loser_id": "new-1", "reason": "contradiction"}]}'
    )
    policy = DefaultConflictPolicy(llm_client=_FakeLLM(inverted))

    verdicts = await policy.confirm_conflicts([(new, old)])
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.winner.id == "new-1"
    assert v.loser.id == "old-1"


@pytest.mark.asyncio
async def test_conflict_verdict_age_passes_through_correct_verdict():
    from prodagent.cognition.memory.conflict import DefaultConflictPolicy
    from prodagent.cognition.memory.storage import MemoryRecord, MemoryType, StoredMemory

    old = StoredMemory.from_record(
        MemoryRecord(content="likes VIP benefits", memory_type=MemoryType.PREFERENCE),
        id="old-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    new = StoredMemory.from_record(
        MemoryRecord(content="downgraded to basic plan", memory_type=MemoryType.PREFERENCE),
        id="new-1",
        created_at="2026-07-01T00:00:00+00:00",
    )
    correct = (
        '{"conflicts": [{"winner_id": "new-1", "loser_id": "old-1", "reason": "contradiction"}]}'
    )
    policy = DefaultConflictPolicy(llm_client=_FakeLLM(correct))

    verdicts = await policy.confirm_conflicts([(new, old)])
    assert len(verdicts) == 1
    assert verdicts[0].winner.id == "new-1"
    assert verdicts[0].loser.id == "old-1"


@pytest.mark.asyncio
async def test_conflict_policy_handles_reasoning_preamble():
    from prodagent.cognition.memory.conflict import DefaultConflictPolicy
    from prodagent.cognition.memory.storage import MemoryRecord, MemoryType, StoredMemory

    old = StoredMemory.from_record(
        MemoryRecord(content="likes VIP benefits", memory_type=MemoryType.PREFERENCE),
        id="old-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    new = StoredMemory.from_record(
        MemoryRecord(content="downgraded to basic plan", memory_type=MemoryType.PREFERENCE),
        id="new-1",
        created_at="2026-07-01T00:00:00+00:00",
    )
    body = (
        "Let me analyze the candidate pairs to determine which truly conflict.\n\n"
        "```json\n"
        '{"conflicts": [{"winner_id": "new-1", "loser_id": "old-1", "reason": "contradiction"}]}\n'
        "```\n"
    )
    policy = DefaultConflictPolicy(llm_client=_FakeLLM(body))

    verdicts = await policy.confirm_conflicts([(new, old)])
    assert len(verdicts) == 1
    assert verdicts[0].winner.id == "new-1"
    assert verdicts[0].loser.id == "old-1"


@pytest.mark.asyncio
async def test_conflict_policy_falls_back_to_reasoning_content():
    from prodagent.cognition.memory.conflict import DefaultConflictPolicy
    from prodagent.cognition.memory.storage import MemoryRecord, MemoryType, StoredMemory

    old = StoredMemory.from_record(
        MemoryRecord(content="likes VIP benefits", memory_type=MemoryType.PREFERENCE),
        id="old-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    new = StoredMemory.from_record(
        MemoryRecord(content="downgraded to basic plan", memory_type=MemoryType.PREFERENCE),
        id="new-1",
        created_at="2026-07-01T00:00:00+00:00",
    )

    class _ReasoningLLM:
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            class R:
                content = ""
                reasoning_content = '{"conflicts": [{"winner_id": "new-1", "loser_id": "old-1", "reason": "contradiction"}]}'

            return R()

    policy = DefaultConflictPolicy(llm_client=_ReasoningLLM())

    verdicts = await policy.confirm_conflicts([(new, old)])
    assert len(verdicts) == 1
    assert verdicts[0].winner.id == "new-1"


class TestLazyPrimitives:
    def test_lock_is_none_until_first_use(self, tmp_path):
        mgr = make_store(tmp_path)
        assert mgr._write_lock is None

    def test_queue_is_none_until_first_use(self, tmp_path):
        mgr = make_store(tmp_path)
        assert mgr._touch_worker._queue is None

    async def test_lock_materialises_on_recall(self, tmp_path):
        mgr = make_store(tmp_path)
        await mgr.recall("anything")
        assert mgr._write_lock is not None

    async def test_queue_materialises_on_touch(self, tmp_path):
        mgr = make_store(tmp_path)
        await mgr.add_memory(
            MemoryRecord(content="prefers dark mode", memory_type=MemoryType.PREFERENCE)
        )
        await mgr.recall("dark mode")
        assert mgr._touch_worker._queue is not None


class TestRecallClassifyLockConsistency:
    async def test_recall_runs_concurrently_without_error(self, tmp_path):
        import asyncio

        mgr = make_store(
            tmp_path,
            classifier=MemoryClassifier(
                _FakeLLM('{"type": "preference", "content": "user likes tea", "confidence": 0.9}')
            ),
        )

        run = _reactive_run("user likes tea", "user likes tea", "user likes tea")

        async def recall_loop():
            for _ in range(20):
                await mgr.recall("tea")

        async def classify_once():
            await mgr.classify(run=run, state="completed")

        await asyncio.gather(recall_loop(), recall_loop(), classify_once())
