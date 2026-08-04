"""REPL slash commands — read-only inspection of agent state, plus /resume and /compact."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.core.state.run import AgentRun
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

_TASK_PREVIEW = 80
_PARAM_PREVIEW = 60
_OUTPUT_PREVIEW = 200
_DESC_PREVIEW = 100
_FACT_PREVIEW = 90
_FACT_LIST_LIMIT = 20

CommandHandler = Callable[["Agent", "AgentRun | None"], Awaitable[str | None]]


class CommandAborted(Exception):
    """User cancelled an inline prompt (Ctrl-C / Ctrl-D)."""


class CommandRegistry:
    """Maps ``/name`` → ``(description, handler)``. Agent-agnostic."""

    def __init__(self) -> None:
        self._commands: dict[str, tuple[str, CommandHandler]] = {}

    def register(self, name: str, description: str, handler: CommandHandler) -> None:
        self._commands[name] = (description, handler)

    def get(self, name: str) -> tuple[str, CommandHandler] | None:
        return self._commands.get(name)

    def names(self) -> list[str]:
        return sorted(self._commands)

    def descriptions(self) -> list[tuple[str, str]]:
        return [(n, self._commands[n][0]) for n in self.names()]


async def _list_run_ids(agent: Agent) -> list[str]:
    """List run_ids via the CheckpointStore protocol (no private-attr access)."""
    ckpt = agent._ensure_checkpoint_resolved()
    if ckpt is None:
        return []
    try:
        ids = await ckpt.list_run_ids()
        return sorted(ids, reverse=True)
    except (OSError, KeyError) as exc:
        logger.debug("list_run_ids failed: %s", exc)
        return []


async def read_resume_task(agent: Agent, run_id: str) -> str:
    """Read run.task from any CheckpointStore via load()."""
    ckpt = agent._ensure_checkpoint_resolved()
    if ckpt is None:
        return "(task not found)"
    try:
        run = await ckpt.load(run_id)
    except (OSError, KeyError) as exc:
        logger.debug("checkpoint load failed for %s: %s", run_id, exc)
        return "(task not found)"
    if run is None:
        return "(task not found)"
    task = getattr(run, "task", "") or ""
    preview = task.replace("\n", " ").strip()
    return preview[:_TASK_PREVIEW] + ("…" if len(preview) > _TASK_PREVIEW else "")


async def _input(prompt: str) -> str:
    """input() for async handlers; raises CommandAborted on EOF/Ctrl-C."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise CommandAborted("user cancelled inline prompt") from exc


async def cmd_resume(agent: Agent, last_run: AgentRun | None) -> str | None:
    """/resume — pick a previous run, return its run_id for the caller to resume."""
    runs = await _list_run_ids(agent)
    if not runs:
        print("  No previous runs found in checkpoint store.")
        return None
    print("  Previous runs (newest first):")
    for i, rid in enumerate(runs, 1):
        task = await read_resume_task(agent, rid)
        print(f"    {i}. {rid}")
        print(f"       task: {task}")
    print()
    choice = await _input("Resume which? (number, or Enter to cancel) > ")
    if not choice:
        return None
    try:
        idx = int(choice)
        if 1 <= idx <= len(runs):
            return runs[idx - 1]
    except ValueError:
        pass
    if choice in runs:
        return choice
    print(f"  '{choice}' is not a known run.")
    return None


async def cmd_status(agent: Agent, last_run: AgentRun | None) -> None:
    if last_run is None:
        print("  No run yet. Type a task to start one.")
        return
    print(f"  run_id:          {last_run.run_id}")
    print(f"  state:           {last_run.state.value}")
    print(f"  turns:           {last_run.turn_count}")
    print(f"  cost:            ${last_run.cost_usd:.4f}")
    print(f"  input_tokens:    {last_run.input_tokens}")
    print(f"  output_tokens:   {last_run.output_tokens}")
    print(f"  tool_history ({len(last_run.tool_history)} calls):")
    for tc in last_run.tool_history:
        params_short = str(tc.params)[:_PARAM_PREVIEW].replace("\n", " ")
        print(f"    {tc.name}({params_short})")
    if last_run.final_output:
        raw_output = str(last_run.final_output)
        preview = raw_output[:_OUTPUT_PREVIEW].replace("\n", " ")
        print(f"  final_output:    {preview}{'…' if len(raw_output) > _OUTPUT_PREVIEW else ''}")


async def cmd_tools(agent: Agent, last_run: AgentRun | None) -> None:
    registry = agent.tool_registry
    mcp_configs = agent.mcp_configs

    if registry is None and not mcp_configs:
        print("  No tool registry configured.")
        return

    from prodagent.core.types import SideEffectLevel

    level_icon = {
        SideEffectLevel.LOW: "LOW",
        SideEffectLevel.MEDIUM: "MED",
        SideEffectLevel.HIGH: "HIGH",
    }

    if registry is not None:
        print("  Registered tools:")
        print(f"  {'name':<24} {'level':<6} {'rev':<6} {'domain':<14} role")
        print(f"  {'─' * 24} {'─' * 6} {'─' * 6} {'─' * 14} {'─' * 10}")
        for role in ("investigate", "remediate", "general"):
            try:
                tools = await registry.get_active_tools(role=role)
            except (KeyError, OSError) as exc:
                logger.debug("get_active_tools(%s) failed: %s", role, exc)
                continue
            if not tools:
                continue
            for t in tools:
                m = t.meta
                rev = f"{m.reversibility:.2f}" if m.reversibility is not None else "—"
                print(
                    f"  {m.name:<24} {level_icon.get(m.side_effect_level, '?'):<6} "
                    f"{rev:<6} {(m.domain or '-'):<14} {role}"
                )

    if mcp_configs:
        print()
        print("  MCP servers (tools injected as mcp__<server>__<tool> at run start):")
        for cfg in mcp_configs:
            name = getattr(cfg, "name", "?")
            transport = getattr(cfg, "transport", "?")
            url = getattr(cfg, "url", "") or ""
            loc = f" — {url}" if url else f" — {getattr(cfg, 'command', '')}"
            print(f"    {name:<20} [{transport}]{loc}")


async def cmd_skills(agent: Agent, last_run: AgentRun | None) -> None:
    skills = agent.skills
    if skills is None:
        print("  No skill registry configured.")
        return
    cards_attr = skills.cards
    cards = (
        cards_attr
        if isinstance(cards_attr, list)
        else (cards_attr() if callable(cards_attr) else [])
    )
    if not cards:
        print("  No skills found.")
        return
    skills_dir = getattr(skills, "root", None) or getattr(skills, "_dir", "(unknown)")
    print(f"  Skills ({len(cards)} loaded from {skills_dir}):")
    for c in cards:
        tags = ", ".join(c.tags) if hasattr(c, "tags") and c.tags else ""
        desc = getattr(c, "description", "") or ""
        print(f"    {c.name}")
        if desc:
            print(f"      {desc[:_DESC_PREVIEW]}")
        if tags:
            print(f"      tags: {tags}")


async def cmd_memory(agent: Agent, last_run: AgentRun | None) -> None:
    manager = agent.memory_manager
    if manager is None:
        print("  No memory manager attached (no MemoryHooks bundle found).")
        return
    # _documents and _facts share a directory; dump from _documents.
    store = getattr(manager, "_documents", None)
    if store is None:
        print("  Memory manager has no document store to dump.")
        return

    store_dir = getattr(store, "root", None) or getattr(store, "_dir", None)
    if store_dir is None:
        print("  Store has no directory attribute — cannot dump.")
        return
    store_path = Path(store_dir)
    print(f"  Memory store: {store_path}")
    print()
    for rel, label in (
        ("memories_soft.json", "Memories (constraints + soft)"),
        ("facts.json", "Facts"),
    ):
        path = store_path / rel
        if not path.exists():
            print(f"  {label}: (file not found: {path.name})")
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"  {label}: (parse error: {exc})")
            continue
        if not data:
            print(f"  {label}: (empty)")
            continue
        print(f"  {label} ({len(data)} entries):")
        for entry in data:
            if isinstance(entry, dict):
                mt = entry.get("memory_type", "?")
                content = entry.get("content", "")
                eid = entry.get("entity_id", "")
                superseded = entry.get("superseded", False)
                version = entry.get("version", 1)
                line = f"    [{mt}] {content[:_FACT_PREVIEW]}"
                if eid:
                    line += f"  (entity: {eid}, v={version})"
                if superseded:
                    line += "  [SUPERSEDED]"
                print(line)
            else:
                print(f"    {entry}")
        print()


async def cmd_context(agent: Agent, last_run: AgentRun | None) -> None:
    fw_config = agent.framework_config
    ctx_cfg = getattr(fw_config, "context", None) if fw_config is not None else None
    if ctx_cfg is None:
        print("  No framework config / context config set.")
        return
    print("  Context window config:")
    print(f"    max_tokens:            {ctx_cfg.max_tokens}")
    print(
        f"    tool_compress_at:      {ctx_cfg.tool_compress_at:.2f}  (fires at ~{int(ctx_cfg.max_tokens * ctx_cfg.tool_compress_at)} tok)"
    )
    print(
        f"    history_summary_at:    {ctx_cfg.history_summary_at:.2f}  (fires at ~{int(ctx_cfg.max_tokens * ctx_cfg.history_summary_at)} tok)"
    )
    print(
        f"    topic_summary_at:      {ctx_cfg.topic_summary_at:.2f}  (fires at ~{int(ctx_cfg.max_tokens * ctx_cfg.topic_summary_at)} tok)"
    )
    print(
        f"    emergency_at:          {ctx_cfg.emergency_at:.2f}  (fires at ~{int(ctx_cfg.max_tokens * ctx_cfg.emergency_at)} tok)"
    )
    print(f"    summary_max_tokens:    {ctx_cfg.summary_max_tokens}")
    print()
    if last_run is None:
        print("  (no run yet — context window is empty)")
        return
    print("  Current run context:")
    print(f"    messages:              {len(last_run.messages)}")
    print(f"    tool_history:          {len(last_run.tool_history)} calls")
    print(f"    turn_count:            {last_run.turn_count}")
    try:
        from prodagent.cognition.context.budget import TokenCounter

        counter = TokenCounter()
        total = sum(counter.count(str(m)) for m in last_run.messages)
        system = counter.count(agent.system_prompt or "")
        print(f"    system prompt tokens:  ~{system}")
        print(f"    message tokens:        ~{total}")
        print(f"    total (sys + msgs):    ~{system + total} / {ctx_cfg.max_tokens}")
        pct = (system + total) / ctx_cfg.max_tokens * 100 if ctx_cfg.max_tokens else 0
        print(f"    window usage:          {pct:.1f}%")
        if pct > 100 * ctx_cfg.emergency_at:
            print(f"    ⚠ above emergency threshold ({ctx_cfg.emergency_at:.2f})")
        elif pct > 100 * ctx_cfg.topic_summary_at:
            print(f"    ⚠ above topic_summary threshold ({ctx_cfg.topic_summary_at:.2f})")
        elif pct > 100 * ctx_cfg.history_summary_at:
            print(f"    ⚠ above history_summary threshold ({ctx_cfg.history_summary_at:.2f})")
        elif pct > 100 * ctx_cfg.tool_compress_at:
            print(f"    ⚠ above tool_compress threshold ({ctx_cfg.tool_compress_at:.2f})")
    except (ImportError, OSError, KeyError) as exc:
        print(f"    (token counter unavailable: {exc})")


async def cmd_compact(agent: Agent, last_run: AgentRun | None) -> None:
    if last_run is None:
        print("  No run to compact — start one first.")
        return
    fw_config = agent.framework_config
    if fw_config is None or getattr(fw_config, "context", None) is None:
        print("  No framework config — can't force compression.")
        return
    import dataclasses

    ctx = fw_config.context
    fw_config.context = dataclasses.replace(
        ctx,
        tool_compress_at=0.01,
        history_summary_at=0.02,
        topic_summary_at=0.03,
        emergency_at=0.04,
    )
    msg_count = len(last_run.messages)
    print(f"  Compression forced. Next turn will compress {msg_count} messages.")
    print(f"  Thresholds lowered to ~1-4% of {ctx.max_tokens} tokens.")
    print("  Watch for TOOL_COMPRESS / HISTORY_SUMMARY / TOPIC_SUMMARY events.")


async def cmd_help(agent: Agent, last_run: AgentRun | None) -> None:
    print("  Slash commands:")
    for name, desc in _default_registry.descriptions():
        print(f"    /{name:<8} {desc}")
    print("  Or type a task description to run the agent.")
    print()


_default_registry = CommandRegistry()
_default_registry.register("resume", "Pick a previous run and resume it", cmd_resume)
_default_registry.register("status", "Show the last run: state, turns, cost, tools", cmd_status)
_default_registry.register("tools", "List registered tools with safety metadata", cmd_tools)
_default_registry.register(
    "skills", "List skill cards (progressive-disclosure runbooks)", cmd_skills
)
_default_registry.register(
    "memory", "Dump the memory store (constraints + memories + facts)", cmd_memory
)
_default_registry.register("context", "Show context window config + current usage", cmd_context)
_default_registry.register("compact", "Force context compression on the next turn", cmd_compact)
_default_registry.register("help", "List available commands", cmd_help)


def get_default_registry() -> CommandRegistry:
    return _default_registry
