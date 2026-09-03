"""RunnerPort end-to-end — real activations driven through the port.

A session-scoped member activation streams the agent's own chat loop
(``InProcessChatRunner``), and a bare spawn-style activation runs a forked
child to terminal state — both through ``RunnerPort.activate``, the
execution-position seam. (The batch dispatch modes — serial / concurrent /
single_winner — left with their only consumer, the stage driver, in
2026-09-02; first-winner races re-emerge as Parallel joins.)
"""

from __future__ import annotations

from prodagent import Agent
from prodagent.kernel.run import RunState, collect_final_run
from prodagent.kernel.types import LLMResponse, RunCompletedEvent
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.ports.execution import AgentActivation, InProcessChatRunner
from prodagent.runtime.config import AgentConfig
from prodagent.runtime.parent_runtime import ParentRuntime
from prodagent.runtime.runner import InProcessRunner


def _member(name: str, *, text: str) -> Agent:
    return Agent(
        name,
        config=AgentConfig(
            name=name,
            llm=FakeLLMAdapter(responses=[LLMResponse(content=text, stop_reason="end_turn")]),
        ),
    )


async def _activate_chat(runner: InProcessChatRunner, agent: Agent) -> str | None:
    """One member activation through the port; returns the terminal output."""
    async for event in runner.activate(
        AgentActivation(agent=agent, task="say your line", session_id=f"s-{agent.name}")
    ):
        if isinstance(event, RunCompletedEvent):
            return event.run.final_output
    return None


async def test_session_activation_streams_member_through_port() -> None:
    """A session-scoped member speaks as itself: no fork, no ledger — the
    agent's own chat loop streamed through the port."""
    runner = InProcessChatRunner()
    agent = _member("a", text="alpha")

    assert await _activate_chat(runner, agent) == "alpha"


async def test_bare_activation_runs_forked_child_through_port() -> None:
    """Spawn-shaped: parent_run_id set, bound runner — the child forks under
    the hop wiring and runs to a terminal event through the port."""
    child = _member("child", text="child done")
    runner = InProcessRunner(ParentRuntime(parent_run_id="root", llm=child.config.llm))

    run = await collect_final_run(
        runner.activate(
            AgentActivation(
                agent=child,
                task="do the sub-task",
                run_id="root::child",
                parent_run_id="root",
                depth=1,
            )
        ),
        fallback_run_id="root::child",
        fallback_task="do the sub-task",
    )

    assert run.state is RunState.COMPLETED
    assert run.final_output == "child done"
