"""A workflow agent's boundary facts land on the tape after U3's change.

Steps left the model's tool menu (they live in the graph now), but an
``llm_step`` still crosses the LLM boundary — and everything that crosses
a boundary gets recorded. This pins the new recording surface; full
plan-mode replay driving is a known gap (the replay deck drives reactive
tapes; see docs/column/gap.md notes).
"""

from __future__ import annotations

import pytest

from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.workflow import Workflow
from prodagent.replay.cassette import derive_cassette
from prodagent.runtime.agent import Agent
from prodagent.runtime.config import AgentConfig


@pytest.mark.asyncio
async def test_llm_step_boundary_facts_land_on_the_cassette(tmp_path):
    from prodagent.backends.factory import in_memory_event_log

    wf = Workflow()
    wf.llm_step("think", prompt="say {{task}}", is_terminal=True)

    log = in_memory_event_log()
    agent = Agent(
        name="wf-tape",
        workflow=wf,
        config=AgentConfig(
            name="wf-tape",
            llm=FakeLLMAdapter(
                responses=[LLMResponse(content="recorded answer", stop_reason="end_turn")]
            ),
            event_log=log,
        ),
    )
    run = await agent.chat("hello tape")

    assert run.state.value == "completed"
    assert "recorded answer" in str(run.final_output)

    cassette = await derive_cassette(log, run.run_id)
    assert cassette is not None
    # the llm node's boundary Q&A is on the tape — recordable means
    # replayable in principle; the boundary the refactor moved (tools out,
    # bodies in) stayed fully observable.
    assert any(r.kind == "llm" for r in cassette.records), (
        f"expected llm boundary facts on the tape, got kinds="
        f"{sorted({r.kind for r in cassette.records})}"
    )
