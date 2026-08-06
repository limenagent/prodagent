"""RunContext — per-hop input, resolved into runtime dependencies on ``__aenter__``."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent.cognition.context.budget import TokenCounter
from prodagent.cognition.context.spill import ToolResultSpillStore

if TYPE_CHECKING:
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.ports.llm import LLMClient
    from prodagent.runtime.agent import Agent


def _resolve_llm(agent: Agent) -> LLMClient:
    from prodagent.backends.factory import resolve_llm
    from prodagent.llm.cache import CachingLLM, CachingLLMClient

    llm = agent.config.llm or resolve_llm(agent.framework_config)
    if isinstance(llm, CachingLLM):
        return llm
    return CachingLLMClient(llm, framework_config=agent.framework_config)


@dataclass
class RunContext:
    """Per-hop input: which agent, what task, which run_id, how deep."""

    agent: Agent
    task: str
    run_id: str
    depth: int = 0
    parent_run_id: str | None = None
    llm: LLMClient = field(init=False)
    checkpoint: CheckpointStore | None = field(init=False, default=None)
    event_log: EventLog | None = field(init=False, default=None)
    spill_store: ToolResultSpillStore | None = field(init=False, default=None)
    stack: contextlib.AsyncExitStack = field(default_factory=contextlib.AsyncExitStack)

    async def __aenter__(self) -> RunContext:
        from prodagent.backends.factory import resolve_checkpoint, resolve_event_log

        fw = self.agent.framework_config
        cfg = self.agent.config

        self.llm = _resolve_llm(self.agent)

        spill_store = cfg.spill_store
        if spill_store is None and getattr(fw.context, "spill_tool_results", False):
            spill_store = ToolResultSpillStore(counter=TokenCounter())
        self.spill_store = spill_store

        self.checkpoint = cfg.checkpoint or resolve_checkpoint(fw)
        self.event_log = cfg.event_log or resolve_event_log(fw)
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stack.aclose()
