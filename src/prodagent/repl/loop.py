"""Interactive REPL loop — prompt, dispatch slash commands, run tasks."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prodagent.hooks.observers._ansi import (
    _BOLD,
    _CYAN,
    _DIM,
    _GREEN,
    _RED,
    _YELLOW,
)
from prodagent.hooks.observers._ansi import (
    colorize as _c,
)

if TYPE_CHECKING:
    from prodagent.core.state.run import AgentRun
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)


async def _cmd_exit(agent: Agent, last_run: AgentRun | None) -> None:
    raise SystemExit(0)


async def _cmd_quit(agent: Agent, last_run: AgentRun | None) -> None:
    raise SystemExit(0)


async def repl_loop(agent: Agent, *, run_id: str | None = None) -> None:
    """Multi-turn REPL: chat() with history when checkpointed, else stateless run() per turn."""
    from prodagent.repl import get_default_registry, read_resume_task
    from prodagent.repl.commands import CommandAborted

    registry = get_default_registry()
    registry.register("exit", "Leave the REPL", _cmd_exit)
    registry.register("quit", "Leave the REPL (alias for /exit)", _cmd_quit)
    last_run: AgentRun | None = None

    import uuid as _uuid

    if run_id is None:
        run_id = str(_uuid.uuid4())
    session_id = run_id
    checkpoint = agent._ensure_checkpoint_resolved()
    has_checkpoint = checkpoint is not None

    print(_c("═" * 72, _BOLD, _CYAN))
    print(_c(f"{agent.name} – Interactive (Ctrl+C to exit)", _BOLD, _CYAN))
    if has_checkpoint:
        print(_c(f"Run {run_id[:8]} — multi-turn (history retained)", _DIM))
    else:
        print(_c("No checkpoint — each turn is stateless", _DIM))
    print(_c("Type /help for commands, or a message to send.", _BOLD, _CYAN))
    print(_c("─" * 72, _BOLD, _CYAN))

    while True:
        try:
            line = input(_c("> ", _GREEN)).strip()
        except (EOFError, KeyboardInterrupt, UnicodeDecodeError):
            print(_c("\nBye.", _DIM))
            return

        if not line:
            continue
        if line.lower() in {"exit", "quit", "q"}:
            return

        if line.startswith("/"):
            cmd_name = line[1:].split()[0].lower() if len(line) > 1 else ""
            entry = registry.get(cmd_name)
            if entry is None:
                print(f"  Unknown command: /{cmd_name}")
                print("  /help for available commands.")
                continue
            _, handler = entry
            try:
                result = await handler(agent, last_run)
            except SystemExit:
                return
            except CommandAborted:
                continue
            except (OSError, KeyError, ValueError) as exc:
                logger.exception("/%s command failed", cmd_name)
                print(f"{_c(f'/{cmd_name} failed:', _RED)} {exc}")
                continue
            # /resume returns a run_id to resume; re-run with it.
            if cmd_name == "resume" and result is not None:
                try:
                    run = await agent.chat(
                        await read_resume_task(agent, result),
                        session_id=result,
                    )
                    last_run = run
                    run_id = result
                except (OSError, KeyError, ValueError) as exc:
                    logger.exception("resume run failed")
                    print(f"{_c('resume failed:', _RED)} {exc}")
            continue

        try:
            run = await agent.chat(line, session_id=session_id)
            last_run = run
            if run.final_output:
                print(run.final_output)
        except KeyboardInterrupt:
            print(_c("Interrupted", _YELLOW) + ". Type 'exit' to quit.")
            continue
        except Exception as exc:
            logger.exception("agent run failed")
            print(f"{_c('ERROR:', _RED)} {exc}")


__all__ = ["repl_loop"]
