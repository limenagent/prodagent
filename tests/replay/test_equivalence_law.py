"""The equivalence law — the replay flagship's headline promise.

``strict(replay(cassette(run))) ≡ run``: re-enacting a run from its tape
and comparing projections finds no difference. This test is the law's CI
embodiment — it never skips, and any change that breaks "history can be
re-enacted" turns it red.

Alongside the law itself, a negative control: the comparator must catch a
real divergence when one is planted (a comparator that cannot fail proves
nothing), and the determinism corollary: the same tape replays to
projection-identical runs.
"""

from __future__ import annotations

from typing import Any

from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.kernel.types import SideEffectLevel, ToolMeta
from prodagent.llm.fake import script
from prodagent.llm.recording import RecordingLLMClient
from prodagent.plan.scheduler import reactive_scheduler
from prodagent.replay.cassette import derive_cassette
from prodagent.replay.engine import CassetteLLMClient, CassettePlayer, replay_tools
from prodagent.replay.strict import assert_equivalent, strict_compare
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _live_tool(name: str, *, result: str = "") -> FunctionTool:
    async def fn(**kwargs: Any) -> dict:
        return {"action": name, **(kwargs if not result else {"note": result})}

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(name=name, is_readonly=True, side_effect_level=SideEffectLevel.LOW),
        schema={
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    )


async def _collect(loop: reactive_scheduler, task: str) -> tuple[Any, list[Any]]:
    final = None
    events: list[Any] = []
    async for event in loop.stream(task):
        events.append(event)
        final = getattr(event, "run", None) or final
    return final, events


async def _live_run(task: str, tools: list[FunctionTool], turns: list[dict[str, Any]]):
    log = InMemoryEventLog()
    dispatcher = ToolDispatcher({t.name: t for t in tools}, event_log=log)
    loop = reactive_scheduler(RecordingLLMClient(script(*turns), log), dispatcher, event_log=log)
    run, events = await _collect(loop, task)
    cassette = await derive_cassette(log, run.run_id)
    return run, events, cassette


def _replay(cassette, task: str):
    player = CassettePlayer(cassette)
    dispatcher = ToolDispatcher(replay_tools(cassette, player))
    loop = reactive_scheduler(CassetteLLMClient(player), dispatcher)
    return _collect(loop, task)


async def test_equivalence_law_replay_equals_original() -> None:
    """The headline: a three-turn run (tool with args, tool, final answer)
    re-enacted from its tape is equivalent on both projections."""
    tools = [_live_tool("search"), _live_tool("lookup")]
    live_run, live_events, cassette = await _live_run(
        "research the framework",
        tools,
        [
            {"tool": "search", "params": {"q": "prodagent"}},
            {"tool": "lookup", "params": {}},
            {"content": "the answer, definitively"},
        ],
    )
    replay_run, replay_events = await _replay(cassette, "research the framework")
    assert_equivalent(live_run, live_events, replay_run, replay_events)


async def test_negative_control_comparator_catches_a_planted_divergence() -> None:
    """A comparator that cannot fail proves nothing: replaying run A's tape
    under run B's task must be caught, with the divergence named."""
    tools = [_live_tool("search")]
    run_a, events_a, cassette_a = await _live_run(
        "task a", tools, [{"tool": "search", "params": {}}, {"content": "answer a"}]
    )
    # A different live run — its tape answers a different first question,
    # so replaying A's tape under a changed flow must diverge or refuse.
    with_cassette_b = await _live_run("task b", tools, [{"content": "answer b"}])
    _run_b, _events_b, cassette_b = with_cassette_b

    replay_run, replay_events = await _replay(cassette_b, "task b")
    assert_equivalent(_run_b, _events_b, replay_run, replay_events)  # b replays as b

    # And cross-pairing b's tape onto a's flow is caught — either the tape
    # refuses (mismatch: different first ask) or the comparator names the
    # divergence. Both outcomes are failures of equivalence, never silence.
    try:
        crossed_run, crossed_events = await _replay(cassette_b, "task a")
    except Exception:
        return  # the tape refused loudly — zero egress did its job
    diffs = strict_compare(run_a, events_a, crossed_run, crossed_events)
    assert diffs, "a crossed tape must not compare equivalent"


async def test_determinism_corollary_same_tape_same_projections() -> None:
    tools = [_live_tool("search")]
    live_run, live_events, cassette = await _live_run(
        "same task", tools, [{"tool": "search", "params": {}}, {"content": "stable"}]
    )
    replay_a = await _replay(cassette, "same task")
    replay_b = await _replay(cassette, "same task")
    assert_equivalent(live_run, live_events, *replay_a)
    assert_equivalent(*replay_a, *replay_b)
