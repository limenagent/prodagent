"""Run entry points — ``drive`` / ``drive_stream`` for a fresh or resumed run."""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import TYPE_CHECKING

from prodagent.core.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.core.state.run import AgentRun, make_failed_run
from prodagent.runtime.coordination.peer import find_suspended_peer
from prodagent.runtime.coordination.run_loop import RunLoop
from prodagent.runtime.session import RunContext

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pydantic import BaseModel

    from prodagent.core.events import AgentEvent
    from prodagent.core.types import ExecutionMode, MessageList
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)


async def drive_stream(
    agent: Agent,
    task: str,
    *,
    run_id: str | None = None,
    output_schema: type[BaseModel] | None = None,
    forced_mode: ExecutionMode | None = None,
    initial_messages: MessageList | None = None,
    parent_run_id: str | None = None,
) -> AsyncGenerator[AgentEvent, None]:
    """Stream agent events from a fresh or resumed run."""
    root_run_id = run_id or str(_uuid.uuid4())
    initial_ctx = await _resolve_initial_context(agent, root_run_id, task)
    if parent_run_id is not None:
        initial_ctx.parent_run_id = parent_run_id
    loop = RunLoop(
        root_agent=agent,
        initial_ctx=initial_ctx,
        root_run_id=root_run_id,
        output_schema=output_schema,
        forced_mode=forced_mode,
        initial_messages=initial_messages,
    )
    async for event in loop.run():
        yield event


async def drive(
    agent: Agent,
    task: str,
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    forced_mode: ExecutionMode | None = None,
    initial_messages: MessageList | None = None,
) -> AgentRun:
    """Drive an agent to terminal state and return the final run. Used by spawn."""
    root_run_id = run_id or str(_uuid.uuid4())
    stream = drive_stream(
        agent,
        task,
        run_id=root_run_id,
        forced_mode=forced_mode,
        initial_messages=initial_messages,
        parent_run_id=parent_run_id,
    )
    return await collect_final_run(stream, fallback_run_id=root_run_id, fallback_task=task)


async def collect_final_run(
    stream: AsyncGenerator[AgentEvent, None],
    *,
    fallback_run_id: str,
    fallback_task: str,
) -> AgentRun:
    final_run: AgentRun | None = None
    async for event in stream:
        if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
            final_run = event.run
    if final_run is None:
        return make_failed_run(fallback_run_id, fallback_task)
    return final_run


async def _resolve_initial_context(agent: Agent, root_run_id: str, task: str) -> RunContext:
    """Pick fresh-start vs peer-resume based on checkpoint state."""
    resume_peer = await find_suspended_peer(agent.checkpoint, root_run_id)
    if resume_peer is not None:
        return await _resume_peer_context(agent, root_run_id, resume_peer)
    return RunContext(agent=agent, task=task, run_id=root_run_id, depth=0)


async def _resume_peer_context(
    agent: Agent, root_run_id: str, resume_peer: tuple[str, str]
) -> RunContext:
    peer_name, peer_run_id = resume_peer
    peer_spec = agent.peer_named(peer_name)
    if peer_spec is None:
        logger.warning(
            "[orchestrator] suspended peer %r not on agent %r — falling back to fresh",
            peer_name,
            agent.name,
        )
        return RunContext(agent=agent, task="", run_id=root_run_id, depth=0)
    logger.info(
        "[orchestrator] resuming suspended peer %r (run_id=%s)",
        peer_name,
        peer_run_id,
    )
    return RunContext(
        agent=peer_spec.fork_as_peer(agent, root_run_id),
        task="",
        run_id=peer_run_id,
        depth=1,
    )
