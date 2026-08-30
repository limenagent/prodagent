"""Replay-engine laws — the tape answers, nothing else does.

Law 1 (re-enactment): a run re-driven with the tape as its outside world
reaches the same terminal state, output, and tool history as the recorded
run — and consumes the tape exactly.

Law 2 (determinism): the same tape replays to the same result, twice.

Law 3 (zero egress): an ask the tape cannot answer fails closed — a
different question, or a tape run dry, raises with both sides named.
There is no fallback path to anything live.

Law 4 (outcome coverage): the ok path replays; lifecycles the engine does
not re-enact yet refuse loudly rather than approximate.
"""

from __future__ import annotations

from typing import Any

import pytest

from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.kernel.loop import ReactiveLoop
from prodagent.kernel.types import SideEffectLevel, ToolMeta
from prodagent.llm.fake import script
from prodagent.llm.recording import RecordingLLMClient
from prodagent.replay.cassette import CassetteMismatch, derive_cassette
from prodagent.replay.engine import CassetteLLMClient, CassettePlayer, replay_tools
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _live_tool(name: str) -> FunctionTool:
    async def fn(**_: Any) -> dict:
        return {"action": name}

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


async def _final(loop: ReactiveLoop) -> Any:
    final = None
    async for event in loop.stream("do the thing"):
        final = getattr(event, "run", None) or final
    return final


async def _live_run() -> tuple[Any, Any]:
    log = InMemoryEventLog()
    dispatcher = ToolDispatcher({"probe": _live_tool("probe")}, event_log=log)
    loop = ReactiveLoop(
        RecordingLLMClient(script({"tool": "probe", "params": {}}, {"content": "all done"}), log),
        dispatcher,
        event_log=log,
    )
    live = await _final(loop)
    cassette = await derive_cassette(log, live.run_id)
    return live, cassette


def _replay(cassette) -> tuple[Any, CassettePlayer]:
    player = CassettePlayer(cassette)
    dispatcher = ToolDispatcher(replay_tools(cassette, player))
    loop = ReactiveLoop(CassetteLLMClient(player), dispatcher)
    return loop, player


async def test_reenactment_law_same_terminal_state_and_history() -> None:
    live, cassette = await _live_run()
    loop, player = _replay(cassette)
    replayed = await _final(loop)

    assert replayed.final_output == live.final_output
    assert replayed.state == live.state
    assert replayed.turn_count == live.turn_count
    assert [c.name for c in replayed.tool_history] == [c.name for c in live.tool_history]
    # The tape is consumed exactly — no record left unplayed, none re-asked.
    assert player.exhausted("llm") and player.exhausted("tool")


async def test_determinism_law_same_tape_same_result() -> None:
    _, cassette = await _live_run()
    first, _p1 = _replay(cassette)
    second, _p2 = _replay(CassettePlayer(cassette).cassette)
    run_a = await _final(first)
    run_b = await _final(second)
    assert run_a.final_output == run_b.final_output
    assert run_a.tool_history == run_b.tool_history


async def test_zero_egress_law_different_ask_fails_closed() -> None:
    _, cassette = await _live_run()
    player = CassettePlayer(cassette)
    dispatcher = ToolDispatcher(replay_tools(cassette, player))
    # A different task makes the first ask differ from the tape's first
    # record — the mismatch must name both sides and raise out of the run.
    loop = ReactiveLoop(CassetteLLMClient(player), dispatcher)
    with pytest.raises(CassetteMismatch):
        async for _ in loop.stream("a materially different task"):
            pass


async def test_zero_egress_law_dry_tape_names_the_fallback_refusal() -> None:
    _, cassette = await _live_run()
    player = CassettePlayer(cassette)
    # No "random" records exist on any tape — the ask runs it dry, and
    # the refusal names the no-fallback contract.
    with pytest.raises(CassetteMismatch, match="no fallback"):
        player.answer("random", "0" * 64)


async def test_non_ok_outcomes_reconstruct_their_tool_results() -> None:
    """The outcome matrix: every recorded lifecycle settles back into the
    ToolResult it was — a replayed suspension re-suspends with its approval
    correlation, a replayed handoff hands off, a replayed error shows the
    model the same structured feedback."""
    from prodagent.kernel.types import ToolOutcome
    from prodagent.replay.engine import _settle

    suspended = _settle(
        {
            "outcome": "suspended",
            "value": None,
            "reason": "awaiting approval",
            "approval_request_id": "req-7",
            "error_detail": None,
        }
    )
    assert suspended.outcome is ToolOutcome.SUSPENDED
    assert suspended.approval_request_id == "req-7"

    handoff = _settle(
        {
            "outcome": "handoff",
            "value": None,
            "reason": "",
            "handoff": {"peer": "reviewer", "task": "t"},
            "error_detail": None,
        }
    )
    assert handoff.outcome is ToolOutcome.HANDOFF
    assert handoff.handoff == {"peer": "reviewer", "task": "t"}

    # Errors ride the retry/abort outcomes with a structured ToolError.
    errored = _settle(
        {
            "outcome": "retry",
            "value": None,
            "error_detail": {
                "reason": "transient",
                "code": "boom",
                "message": "it broke",
                "hint": "",
                "error_severity": "yellow",
            },
        }
    )
    assert errored.outcome is ToolOutcome.RETRY
    assert errored.error.code == "boom" and errored.error.message == "it broke"
