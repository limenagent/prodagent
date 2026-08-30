"""Phase 7 safety gate: version history/rollback, tag-overlap fix, correctness
gate, and rate limiting for the skill self-evolution loop.

The loop could silently overwrite a live skill file with
no way back, no correctness check beyond "the run completed", and a
mismatched tag-overlap threshold that let unrelated skills get merged.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from prodagent import RunState
from prodagent.backends.file.experience import FileExperienceStore
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.ports.persistence import ExperienceRecord
from prodagent.skills.registry import SkillCard, SkillContent, SkillRegistry
from prodagent.skills.skill_synthesizer import SkillSynthesizer

if TYPE_CHECKING:
    from pathlib import Path


def _fake_llm(*contents: str) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        responses=[LLMResponse(content=c, stop_reason="end_turn") for c in contents]
    )


def _seed(tags: list[str], run_id: str = "r") -> ExperienceRecord:
    from prodagent.kernel.types import ToolCall

    run = AgentRun(run_id=run_id, task="do the thing", state=RunState.COMPLETED)
    run.tool_history = [ToolCall(name="do_it", params={})]
    run.final_output = "done"
    return ExperienceRecord.from_run(run, tags=tags)


# -- Version history / rollback -------------------------------------------


def test_register_archives_previous_version_before_overwrite(tmp_path: Path):
    reg = SkillRegistry(skills_dir=tmp_path)
    v1 = SkillContent(
        card=SkillCard(name="oom", description="d", version="1.0"), full_doc="v1 body"
    )
    v2 = SkillContent(
        card=SkillCard(name="oom", description="d", version="1.1"), full_doc="v2 body"
    )

    reg.register(v1)
    reg.register(v2)

    history_dir = tmp_path / ".history" / "oom"
    archived = list(history_dir.glob("*_v1.0.md"))
    assert len(archived) == 1
    assert "v1 body" in archived[0].read_text(encoding="utf-8")

    current = (tmp_path / "oom.md").read_text(encoding="utf-8")
    assert "v2 body" in current


def test_rollback_restores_archived_version(tmp_path: Path):
    reg = SkillRegistry(skills_dir=tmp_path)
    v1 = SkillContent(
        card=SkillCard(name="oom", description="d", version="1.0"), full_doc="v1 body"
    )
    v2 = SkillContent(
        card=SkillCard(name="oom", description="d", version="1.1"), full_doc="v2 body"
    )
    reg.register(v1)
    reg.register(v2)

    ok = reg.rollback("oom", "1.0")

    assert ok is True
    restored = reg.get("oom")
    assert restored is not None
    assert restored.card.version == "1.0"
    assert "v1 body" in restored.full_doc
    # rollback is itself a register() call — v1.1 gets archived on the way out.
    assert list((tmp_path / ".history" / "oom").glob("*_v1.1.md"))


def test_rollback_returns_false_when_version_never_existed(tmp_path: Path):
    reg = SkillRegistry(skills_dir=tmp_path)
    reg.register(
        SkillContent(card=SkillCard(name="oom", description="d", version="1.0"), full_doc="x")
    )

    assert reg.rollback("oom", "9.9") is False
    assert reg.rollback("never-registered", "1.0") is False


def test_rollback_false_for_in_memory_registry_without_skills_dir():
    reg = SkillRegistry()  # no skills_dir — nothing to archive or roll back
    reg.register(
        SkillContent(card=SkillCard(name="oom", description="d", version="1.0"), full_doc="x")
    )
    reg.register(
        SkillContent(card=SkillCard(name="oom", description="d", version="1.1"), full_doc="y")
    )

    assert reg.rollback("oom", "1.0") is False


# -- Tag-overlap threshold fix ----------------------------------------------


@pytest.mark.asyncio
async def test_single_shared_tag_does_not_merge_into_unrelated_skill():
    """Two skills sharing exactly one tag must stay two separate skills —
    the historical bug: skill_synthesizer used min_tag_overlap=1 while
    registry.find_similar defaulted to 2, so a lone shared tag was enough to
    silently fold a new skill into an unrelated existing one."""
    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="skill-a", description="d", version="1.0", tags=["alpha", "common"]
            ),
            full_doc="skill-a body",
        )
    )

    new_skill_json = json.dumps(
        {
            "name": "skill-b",
            "description": "unrelated procedure",
            "version": "1.0",
            "tags": ["common", "beta"],
            "procedure": "1. do beta things",
        }
    )
    synth = SkillSynthesizer(_fake_llm(new_skill_json), registry)
    seed = _seed(tags=["common"])  # overlaps skill-a by exactly 1 tag

    result = await synth.maybe_synthesize(seed)

    assert result.skill is not None
    assert result.skill.card.name == "skill-b"
    assert registry.get("skill-a") is not None
    assert registry.get("skill-a").card.version == "1.0"  # untouched
    assert registry.get("skill-b") is not None


# -- Correctness gate: overwrite needs corroborating successes --------------


@pytest.mark.asyncio
async def test_overwrite_blocked_without_corroborating_successes(tmp_path: Path):
    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="existing", description="d", version="1.0", tags=["pod", "kubernetes"]
            ),
            full_doc="old body",
        )
    )
    llm = _fake_llm(
        json.dumps(
            {
                "name": "existing",
                "description": "d",
                "version": "1.1",
                "tags": ["pod", "kubernetes"],
                "procedure": "new",
            }
        )
    )
    synth = SkillSynthesizer(llm, registry, store=store)

    seed = _seed(tags=["pod", "kubernetes"])  # no prior corroborating record in store
    result = await synth.maybe_synthesize(seed)

    assert result.skill is None
    assert result.failure.startswith("insufficient corroboration")
    assert llm.call_count == 0, "gate must block before spending an LLM call"
    assert registry.get("existing").card.version == "1.0"  # untouched


@pytest.mark.asyncio
async def test_overwrite_allowed_once_corroborating_successes_exist(tmp_path: Path):
    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="existing", description="d", version="1.0", tags=["pod", "kubernetes"]
            ),
            full_doc="old body",
        )
    )
    patched = json.dumps(
        {
            "name": "existing",
            "description": "d",
            "version": "1.1",
            "tags": ["pod", "kubernetes"],
            "procedure": "new",
        }
    )
    llm = _fake_llm(patched)
    synth = SkillSynthesizer(llm, registry, store=store)

    prior = _seed(tags=["pod", "kubernetes"], run_id="prior")
    await store.record(prior)
    await asyncio.sleep(0.02)

    seed = _seed(tags=["pod", "kubernetes"], run_id="seed")
    await store.record(seed)  # LearningHooks records before synthesizing
    await asyncio.sleep(0.02)

    result = await synth.maybe_synthesize(seed)

    assert result.skill is not None
    assert result.skill.card.version == "1.1"
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_new_skill_creation_is_not_gated_by_corroboration(tmp_path: Path):
    """Creating a brand-new skill stays lenient — only *overwriting* an
    existing one needs corroboration."""
    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    llm = _fake_llm(
        json.dumps(
            {"name": "fresh", "description": "d", "version": "1.0", "tags": ["x"], "procedure": "p"}
        )
    )
    synth = SkillSynthesizer(llm, registry, store=store)

    result = await synth.maybe_synthesize(_seed(tags=["x"]))

    assert result.skill is not None
    assert llm.call_count == 1


# -- Rate limiting: cooldown per skill name ---------------------------------


@pytest.mark.asyncio
async def test_second_overwrite_within_cooldown_is_rate_limited():
    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="existing", description="d", version="1.0", tags=["pod", "kubernetes"]
            ),
            full_doc="old body",
        )
    )
    patch_json = json.dumps(
        {
            "name": "existing",
            "description": "d",
            "version": "1.1",
            "tags": ["pod", "kubernetes"],
            "procedure": "p",
        }
    )
    llm = _fake_llm(patch_json, patch_json)
    synth = SkillSynthesizer(llm, registry, overwrite_cooldown_seconds=999.0)

    first = await synth.maybe_synthesize(_seed(tags=["pod", "kubernetes"], run_id="r1"))
    assert first.skill is not None
    assert llm.call_count == 1

    second = await synth.maybe_synthesize(_seed(tags=["pod", "kubernetes"], run_id="r2"))
    assert second.skill is None
    assert second.failure.startswith("rate_limited")
    assert llm.call_count == 1, "rate-limited overwrite must not spend another LLM call"


@pytest.mark.asyncio
async def test_overwrite_allowed_again_after_cooldown_elapses():
    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="existing", description="d", version="1.0", tags=["pod", "kubernetes"]
            ),
            full_doc="old body",
        )
    )
    patch_json = json.dumps(
        {
            "name": "existing",
            "description": "d",
            "version": "1.1",
            "tags": ["pod", "kubernetes"],
            "procedure": "p",
        }
    )
    llm = _fake_llm(patch_json, patch_json)
    synth = SkillSynthesizer(llm, registry, overwrite_cooldown_seconds=0.01)

    first = await synth.maybe_synthesize(_seed(tags=["pod", "kubernetes"], run_id="r1"))
    assert first.skill is not None

    await asyncio.sleep(0.02)

    second = await synth.maybe_synthesize(_seed(tags=["pod", "kubernetes"], run_id="r2"))
    assert second.skill is not None
    assert llm.call_count == 2
