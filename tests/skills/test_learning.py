from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from prodagent import RunState
from prodagent.backends.file.experience import FileExperienceStore
from prodagent.hooks.bundles.learning import LearningHooks
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import LLMResponse, MessageList, ToolCall
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.ports.experience import ExperienceRecord
from prodagent.skills.registry import SkillRegistry
from prodagent.skills.skill_synthesizer import SkillSynthesizer


def _completed_run(run_id: str = "run-1", task: str = "restart the pod") -> AgentRun:
    run = AgentRun(run_id=run_id, task=task, state=RunState.COMPLETED)
    run.tool_history = [ToolCall(name="kubectl_restart", params={"pod": "web-1"})]
    run.final_output = "Pod restarted successfully."
    run.metrics.cost_usd = 0.002
    run.metrics.turn_count = 2
    run.messages = [
        {"role": "user", "content": task},
        {"role": "assistant", "content": "I will check the pod status first."},
        {
            "role": "user",
            "content": "Tool 'kubectl_restart' result: {'status': 'restarted', 'pod': 'web-1'}",
        },
        {"role": "assistant", "content": "Pod restarted successfully."},
    ]
    return run


def _fake_llm(*contents: str) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        responses=[LLMResponse(content=c, stop_reason="end_turn") for c in contents]
    )


def test_experience_record_captures_transcript():
    run = _completed_run()
    rec = ExperienceRecord.from_run(run)
    assert len(rec.session_transcript) == len(run.messages)
    assert rec.session_transcript[0] == {"role": "user", "content": run.task}
    assert rec.session_transcript[1]["role"] == "assistant"
    assert any("kubectl_restart" in m.get("content", "") for m in rec.session_transcript)


def test_experience_record_from_plan_first_run():
    run = AgentRun(run_id="pf-1", task="respond to OOM incident", state=RunState.COMPLETED)
    run.messages = [
        {"role": "user", "content": "respond to OOM incident"},
        {
            "role": "assistant",
            "content": (
                "Step investigate: Root cause: OOM kill from heap growth in "
                "payment-service v2.15.0. Tool: get_logs(service=payment)."
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Step remediate: Rolled back to v2.14.0. SLO recovered within 4 "
                "minutes. Tool: rollback(sha=f8c01d4)."
            ),
        },
    ]

    rec = ExperienceRecord.from_run(run)

    assert len(rec.session_transcript) >= 2
    combined = " ".join(m.get("content", "") for m in rec.session_transcript)
    assert "investigate" in combined
    assert "remediate" in combined
    assert "get_logs" in combined or "rollback" in combined


def test_experience_record_roundtrip():
    rec = ExperienceRecord.from_run(_completed_run())
    loaded = ExperienceRecord.from_dict(json.loads(rec.to_jsonl()))
    assert loaded.run_id == rec.run_id
    assert loaded.outcome == rec.outcome
    assert loaded.tool_sequence == rec.tool_sequence
    assert loaded.session_transcript == rec.session_transcript


def test_experience_record_roundtrip_backward_compat():
    rec = ExperienceRecord.from_run(_completed_run())
    d = json.loads(rec.to_jsonl())
    d.pop("session_transcript")
    loaded = ExperienceRecord.from_dict(d)
    assert loaded.session_transcript == []


def test_experience_record_tag_extraction():
    rec = ExperienceRecord.from_run(
        _completed_run(task="check pod logs for memory errors in production")
    )
    assert "pod" in rec.tags
    assert "logs" in rec.tags
    assert "memory" in rec.tags
    assert "for" not in rec.tags
    assert "in" not in rec.tags


async def test_store_record_and_load(tmp_path: Path):
    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    rec = ExperienceRecord.from_run(_completed_run("run-1"))
    await store.record(rec)

    time.sleep(0.05)

    loaded = await store.load_all()
    assert len(loaded) == 1
    assert loaded[0].run_id == "run-1"


_SKILL_JSON = json.dumps(
    {
        "name": "pod-restart",
        "description": "Restart a Kubernetes pod safely",
        "version": "1.0",
        "tags": ["pod", "kubernetes"],
        "procedure": "1. Check logs\n2. Drain node\n3. Restart pod",
        "pitfalls": "Don't restart without checking logs first.",
        "verification": "Run kubectl get pods and confirm Running state.",
        "contradictions_detected": [],
    }
)

_SKILL_PATCH_JSON = json.dumps(
    {
        "name": "pod-restart",
        "description": "Restart a Kubernetes pod safely",
        "version": "1.1",
        "tags": ["pod", "kubernetes"],
        "procedure": (
            "1. Check logs\n"
            "2. When error_rate>10%: check error_rate dashboard. "
            "When upstream_p99>500ms (new evidence): check upstream timeout.\n"
            "3. Drain node\n4. Restart pod"
        ),
        "pitfalls": "Don't restart without checking logs first.",
        "verification": "Run kubectl get pods and confirm Running state.",
        "contradictions_detected": [
            {
                "old": "PGW 5xx 先查 error_rate",
                "new": "PGW 5xx 先查 upstream timeout",
                "resolution": "保留双分支:when error_rate>10% 查 X,when upstream_p99>500ms 查 Y",
            }
        ],
    }
)


@pytest.mark.asyncio
async def test_patch_first_session_creates_skill():
    registry = SkillRegistry()
    llm = _fake_llm(_SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)

    seed = ExperienceRecord.from_run(_completed_run("r0", task="restart the pod"))
    seed.tags = ["pod"]

    result = await synth.maybe_synthesize(seed)
    skill = result.skill
    assert skill is not None
    assert skill.card.name == "pod-restart"
    assert registry.get("pod-restart") is not None
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_synthesizer_falls_back_to_reasoning_content():
    registry = SkillRegistry()
    llm = FakeLLMAdapter(
        responses=[
            LLMResponse(
                content="",
                reasoning_content=_SKILL_JSON,
                stop_reason="end_turn",
            )
        ]
    )
    synth = SkillSynthesizer(llm, registry)

    seed = ExperienceRecord.from_run(_completed_run("r0", task="restart the pod"))
    seed.tags = ["pod"]

    result = await synth.maybe_synthesize(seed)
    skill = result.skill
    assert skill is not None, "should fall back to reasoning_content"
    assert skill.card.name == "pod-restart"


@pytest.mark.asyncio
async def test_synthesizer_returns_none_when_both_empty():
    registry = SkillRegistry()
    llm = FakeLLMAdapter(
        responses=[LLMResponse(content="", reasoning_content="", stop_reason="max_tokens")]
    )
    synth = SkillSynthesizer(llm, registry)

    seed = ExperienceRecord.from_run(_completed_run("r0", task="restart the pod"))
    seed.tags = ["pod"]

    result = await synth.maybe_synthesize(seed)
    assert result.skill is None


@pytest.mark.asyncio
async def test_patch_second_session_updates_existing():
    from prodagent.skills.registry import SkillCard, SkillContent

    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="pod-restart",
                description="old",
                version="1.0",
                tags=["pod", "kubernetes"],
            ),
            full_doc="## Procedure\n1. old step",
        )
    )
    llm = _fake_llm(_SKILL_PATCH_JSON)
    synth = SkillSynthesizer(llm, registry)

    seed = ExperienceRecord.from_run(_completed_run("r1", task="restart the pod"))
    seed.tags = ["pod"]

    result = await synth.maybe_synthesize(seed)
    skill = result.skill
    assert skill is not None
    assert skill.card.version == "1.1"
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_patch_detects_contradiction():
    from prodagent.skills.registry import SkillCard, SkillContent

    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="pod-restart",
                description="d",
                version="1.0",
                tags=["pod"],
            ),
            full_doc="## Procedure\n1. PGW 5xx 先查 error_rate",
        )
    )
    llm = _fake_llm(_SKILL_PATCH_JSON)
    synth = SkillSynthesizer(llm, registry)

    seed = ExperienceRecord.from_run(_completed_run("r1", task="PGW 5xx incident"))
    seed.tags = ["pod"]

    result = await synth.maybe_synthesize(seed)
    skill = result.skill
    assert skill is not None
    assert len(skill.contradictions) == 1
    c = skill.contradictions[0]
    assert "error_rate" in c["old"]
    assert "upstream timeout" in c["new"]
    assert "双分支" in c["resolution"]
    assert "error_rate" in skill.full_doc
    assert "upstream" in skill.full_doc


@pytest.mark.asyncio
async def test_patch_no_contradiction_when_complementary():
    from prodagent.skills.registry import SkillCard, SkillContent

    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="pod-restart",
                description="d",
                version="1.0",
                tags=["pod"],
            ),
            full_doc="## Procedure\n1. Check logs",
        )
    )
    llm = _fake_llm(_SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)

    seed = ExperienceRecord.from_run(_completed_run("r1", task="restart the pod"))
    seed.tags = ["pod"]

    result = await synth.maybe_synthesize(seed)
    skill = result.skill
    assert skill is not None
    assert skill.contradictions == []


@pytest.mark.asyncio
async def test_patch_preserves_existing_content():
    from prodagent.skills.registry import SkillCard, SkillContent

    registry = SkillRegistry()
    existing_doc = (
        "## Procedure\n1. Check logs\n2. Drain node\n3. Restart pod\n## Pitfalls\nDon't skip logs."
    )
    registry.register(
        SkillContent(
            card=SkillCard(
                name="pod-restart",
                description="d",
                version="1.0",
                tags=["pod"],
            ),
            full_doc=existing_doc,
        )
    )
    patched = json.dumps(
        {
            "name": "pod-restart",
            "description": "Restart a pod",
            "version": "1.1",
            "tags": ["pod"],
            "procedure": "1. Check logs\n2. Drain node\n3. Restart pod\n4. Verify running",
            "pitfalls": "Don't skip logs.",
            "verification": "kubectl get pods",
            "contradictions_detected": [],
        }
    )
    llm = _fake_llm(patched)
    synth = SkillSynthesizer(llm, registry)

    seed = ExperienceRecord.from_run(_completed_run("r1", task="restart the pod"))
    seed.tags = ["pod"]

    result = await synth.maybe_synthesize(seed)
    skill = result.skill
    assert skill is not None
    assert "Check logs" in skill.full_doc
    assert "Drain node" in skill.full_doc
    assert "Verify running" in skill.full_doc
    assert skill.contradictions == []


@pytest.mark.asyncio
async def test_patch_noop_for_failed_run():
    registry = SkillRegistry()
    llm = _fake_llm(_SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)

    failed_run = AgentRun(run_id="f", task="restart pod", state=RunState.FAILED)
    seed = ExperienceRecord.from_run(failed_run)
    seed.tags = ["pod"]

    result = await synth.maybe_synthesize(seed)
    assert result.skill is None
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_synthesizer_sends_full_transcript():
    registry = SkillRegistry()
    captured: list[MessageList] = []

    class _CapturingLLM:
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk, **_):
            captured.append(list(messages))
            return LLMResponse(content=_SKILL_JSON, stop_reason="end_turn")

    synth = SkillSynthesizer(_CapturingLLM(), registry)
    seed = ExperienceRecord.from_run(_completed_run("r-transcript", task="restart the pod"))
    seed.tags = ["pod"]

    await synth.maybe_synthesize(seed)

    assert captured, "LLM was not called"
    prompt_text = captured[0][0]["content"]
    assert "I will check the pod status first." in prompt_text, (
        "agent reasoning turn missing from synthesis prompt"
    )
    assert "kubectl_restart" in prompt_text, "tool result missing from synthesis prompt"
    assert "restarted" in prompt_text, "tool result content missing from synthesis prompt"


@pytest.mark.asyncio
async def test_synthesizer_truncates_tool_result_head_tail():
    from prodagent.skills.skill_synthesizer import _format_session_transcript

    run = AgentRun(run_id="big", task="restart the pod", state=RunState.COMPLETED)
    run.tool_history = [ToolCall(name="kubectl_logs", params={})]
    run.final_output = "done"
    run.metrics.cost_usd = 0.0
    run.metrics.turn_count = 1
    head = "HEAD_MARKER" * 300
    tail = "TAIL_MARKER" * 50
    payload = head + "MIDDLE_NOISE" * 500 + tail
    run.messages = [
        {"role": "user", "content": "restart the pod"},
        {"role": "assistant", "content": "fetching logs"},
        {"role": "user", "content": f"Tool 'kubectl_logs' result: {payload}"},
        {"role": "assistant", "content": "done"},
    ]
    rec = ExperienceRecord.from_run(run)
    text = _format_session_transcript(rec)

    assert "HEAD_MARKER" in text, "head of tool result should be preserved"
    assert "TAIL_MARKER" in text, "tail of tool result should be preserved"
    assert "MIDDLE_NOISE" not in text, "middle should be folded out"
    assert "(middle truncated)" in text, "truncation marker must be present"


@pytest.mark.asyncio
async def test_synthesis_prompt_is_domain_neutral():
    registry = SkillRegistry()
    captured: list[str] = []

    class _CapturingLLM:
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk, **_):
            captured.append(system)
            captured.append(messages[0]["content"])
            return LLMResponse(content=_SKILL_JSON, stop_reason="end_turn")

    synth = SkillSynthesizer(_CapturingLLM(), registry)
    seed = ExperienceRecord.from_run(_completed_run("r1", task="review the pull request"))
    seed.tags = ["review"]
    await synth.maybe_synthesize(seed)

    blob = "\n".join(captured).lower()
    assert "incident" not in blob, "default prompt still domain-specific"
    assert "procedure" in blob


def test_parse_skill_includes_when_to_use():
    synth = SkillSynthesizer(_fake_llm(), SkillRegistry())
    raw = json.dumps(
        {
            "name": "x",
            "description": "d",
            "when_to_use": "TRIGGER-SIGNATURE",
            "procedure": "1. do a thing",
        }
    )
    result = synth._parse_skill(raw, primary_tag="t")
    skill = result.skill
    assert skill is not None
    assert "TRIGGER-SIGNATURE" in skill.full_doc
    assert "When to use" in skill.full_doc


def test_parse_skill_extracts_contradictions():
    synth = SkillSynthesizer(_fake_llm(), SkillRegistry())
    raw = json.dumps(
        {
            "name": "x",
            "description": "d",
            "procedure": "1. ...",
            "contradictions_detected": [
                {"old": "do A", "new": "do B", "resolution": "when X do A, when Y do B"},
            ],
        }
    )
    result = synth._parse_skill(raw, primary_tag="t")
    skill = result.skill
    assert skill is not None
    assert len(skill.contradictions) == 1
    assert skill.contradictions[0]["old"] == "do A"
    assert skill.contradictions[0]["resolution"].startswith("when X")


def test_parse_skill_handles_reasoning_preamble():
    synth = SkillSynthesizer(_fake_llm(), SkillRegistry())
    raw = (
        "Let me analyze the existing skill and the new transcript to identify "
        "contradictions, new learnings, and patches needed.\n\n"
        "```json\n"
        '{"name": "x", "description": "d", "procedure": "1. do a thing"}\n'
        "```\n"
    )
    result = synth._parse_skill(raw, primary_tag="t")
    skill = result.skill
    assert skill is not None
    assert skill.card.name == "x"
    assert "do a thing" in skill.full_doc


@pytest.mark.asyncio
async def test_synthesizer_merges_similar_skill():
    from prodagent.skills.registry import SkillCard, SkillContent

    registry = SkillRegistry()
    registry.register(
        SkillContent(
            card=SkillCard(
                name="existing-pod-skill",
                description="old",
                version="1.0",
                tags=["pod", "kubernetes"],
            ),
            full_doc="old doc",
        )
    )
    synth = SkillSynthesizer(_fake_llm(_SKILL_JSON), registry)
    seed = ExperienceRecord.from_run(_completed_run("r1", task="restart the pod"))
    seed.tags = ["pod"]
    result = await synth.maybe_synthesize(seed)
    skill = result.skill
    assert skill is not None
    assert skill.card.name == "existing-pod-skill"
    assert skill.card.version == "1.1"
    assert registry.get("pod-restart") is None


async def test_improvement_cycle_attaches_to_session_end(tmp_path: Path):
    from prodagent.kernel.bus import HookEvent, HookRegistry

    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    llm = _fake_llm(_SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)
    cycle = LearningHooks(synth, registry=registry, store=store, watch_tags=["pod"])

    hooks = HookRegistry()
    cycle.attach(hooks)

    run = _completed_run("run-attach")
    await hooks.fire(HookEvent.SESSION_END, run=run)
    await asyncio.sleep(0.1)

    all_records = await store.load_all()
    assert any(r.run_id == "run-attach" for r in all_records)
    assert llm.call_count == 1
    assert registry.get("pod-restart") is not None


@pytest.mark.asyncio
async def test_loop_patches_once_per_session(tmp_path: Path):
    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    llm = _fake_llm(_SKILL_JSON, _SKILL_JSON, _SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)
    cycle = LearningHooks(
        synth,
        registry=registry,
        store=store,
        watch_tags=["pod", "oom", "restart", "deploy", "scale"],
    )

    run = _completed_run("run-multi", task="restart the pod after oom")
    await cycle._safely_run_loop(run)

    assert llm.call_count == 1, (
        f"expected 1 patch call, got {llm.call_count} — fan-out bug regressed"
    )


@pytest.mark.asyncio
async def test_loop_watch_tags_filter_skips_non_overlapping(tmp_path: Path):
    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    llm = _fake_llm(_SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)
    cycle = LearningHooks(synth, registry=registry, store=store, watch_tags=["pod", "deploy"])

    run = _completed_run("run-skip", task="review the pull request")
    await cycle._safely_run_loop(run)

    assert llm.call_count == 0, "session outside watch_tags should not patch"
    await asyncio.sleep(0.05)
    assert any(r.run_id == "run-skip" for r in await store.load_all())


@pytest.mark.asyncio
async def test_loop_patches_all_when_watch_tags_empty(tmp_path: Path):
    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    llm = _fake_llm(_SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)
    cycle = LearningHooks(synth, registry=registry, store=store, watch_tags=None)

    run = _completed_run("run-any", task="review the pull request")
    await cycle._safely_run_loop(run)

    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_flush_awaits_pending_background_tasks(tmp_path: Path):
    from prodagent.kernel.bus import HookEvent, HookRegistry

    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    llm = _fake_llm(_SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)
    cycle = LearningHooks(synth, registry=registry, store=store, watch_tags=["pod"])

    hooks = HookRegistry()
    cycle.attach(hooks)

    run = _completed_run("run-flush")
    await hooks.fire(HookEvent.SESSION_END, run=run)
    await cycle.flush(timeout=5.0)

    all_records = await store.load_all()
    assert any(r.run_id == "run-flush" for r in all_records)
    assert llm.call_count == 1
    assert registry.get("pod-restart") is not None


@pytest.mark.asyncio
async def test_flush_noop_when_no_pending_tasks(tmp_path: Path):
    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()
    llm = _fake_llm(_SKILL_JSON)
    synth = SkillSynthesizer(llm, registry)
    cycle = LearningHooks(synth, registry=registry, store=store)

    await cycle.flush(timeout=1.0)
    assert llm.call_count == 0


@pytest.mark.asyncio
async def test_flush_times_out_and_cancels_slow_task(tmp_path: Path):
    from prodagent.kernel.bus import HookEvent, HookRegistry

    store = FileExperienceStore(path=tmp_path / "exp.jsonl")
    registry = SkillRegistry()

    class _HungSynth:
        async def maybe_synthesize(self, rec):
            await asyncio.sleep(60)

    cycle = LearningHooks(
        _HungSynth(),  # type: ignore[arg-type]
        registry=registry,
        store=store,
        watch_tags=["pod"],
    )
    hooks = HookRegistry()
    cycle.attach(hooks)

    run = _completed_run("run-hung")
    await hooks.fire(HookEvent.SESSION_END, run=run)
    await cycle.flush(timeout=0.2)
    assert len(cycle._tasks) == 0
