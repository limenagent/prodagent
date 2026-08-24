"""Coloured terminal output for every agent lifecycle event."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from prodagent.hooks.observers._ansi import (
    _BLUE,
    _BOLD,
    _CYAN,
    _DIM,
    _GREEN,
    _MAGENTA,
    _RED,
    _YELLOW,
)
from prodagent.hooks.observers._ansi import (
    colorize as _c,
)
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.types import Layer

if TYPE_CHECKING:
    from prodagent.kernel.bus import HookRegistry


def is_subagent_run_id(run_id: str) -> bool:
    from prodagent.kernel.state import is_child_run_id

    return bool(run_id) and is_child_run_id(run_id)


def _subagent_suffix(run_id: str) -> str:
    if not is_subagent_run_id(run_id):
        return ""
    return f" ({run_id.rsplit('::', 1)[-1]})"


class ConsoleObserverHooks:
    """Terminal renderer: one private method per ``HookEvent``, dispatched via the
    ``_HANDLERS`` table at the bottom of this file (event -> method name)."""

    def __init__(self, *, verbose: bool = True) -> None:
        self._verbose = verbose
        self._think_started = False
        self._turn = 0

    def attach(self, hooks: HookRegistry) -> None:
        hooks.register_all_events(self.on_event)

    def on_event(self, *, event_name: str = "", **kw: Any) -> None:
        name = _HANDLERS.get(cast("HookEvent", event_name))
        if name is not None:
            getattr(self, name)(**kw)

    def _label(self, tag: str, color: str = _CYAN) -> str:
        return _c(f"  {tag:<14}", color, _BOLD)

    def _dim(self, text: str) -> str:
        return _c(text, _DIM)

    def _session_start(self, *, run_id: str = "", task: str = "", **_: Any) -> None:
        bar = _c("─" * 72, _DIM)
        print(f"\n{bar}")
        print(
            f"{self._label('SESSION', _BLUE)}▶ Starting  run={_c(run_id[:16], _BOLD)}  "
            f"{self._dim(task[:60])}"
        )

    def _session_end(
        self,
        *,
        run_id: str = "",
        state: str = "",
        turns: int = 0,
        cost_usd: float = 0,
        memories_written: int = 0,
        **_: Any,
    ) -> None:
        color = _GREEN if state == "completed" else _RED
        print(
            f"{self._label('SESSION', _BLUE)}■ End  state={_c(state.upper(), color)}  "
            f"{turns} turns  ${cost_usd:.4f}"
            + (
                f"  {_c(f'{memories_written} memories written', _MAGENTA)}"
                if memories_written
                else ""
            )
        )

    @staticmethod
    def _bar(filled: float, width: int = 10) -> str:
        """Render a progress bar: ▓▓▓░░░░░░░. ``filled`` is 0..1."""
        n = max(0, min(width, round(filled * width)))
        return "▓" * n + "░" * (width - n)

    def _context(
        self,
        *,
        system_tokens: int = 0,
        msg_count: int = 0,
        compression: str = "NONE",
        guardrail_layers: int = 0,
        spilled_results: int = 0,
        total_tokens: int = 0,
        layer_tokens: dict[str, int] | None = None,
        pre_history_tokens: int = 0,
        max_tokens: int = 0,
        **_: Any,
    ) -> None:
        layer_tokens = layer_tokens or {}
        l0 = layer_tokens.get(Layer.L0.value, system_tokens)
        l1 = layer_tokens.get(Layer.L1.value, 0)
        l2 = layer_tokens.get(Layer.L2.value, 0)
        l3 = layer_tokens.get(Layer.L3.value, 0)

        pct = (total_tokens / max_tokens * 100) if max_tokens else 0
        total_bar = self._bar(total_tokens / max_tokens) if max_tokens else ""
        compress_str = (
            f"Compress: {_c(compression, _YELLOW, _BOLD)}"
            if compression != "NONE"
            else f"Compress: {self._dim('NONE')}"
        )
        if compression != "NONE" and pre_history_tokens > l3:
            saved = pre_history_tokens - l3
            compress_str += f"  {pre_history_tokens}→{l3} {_c(f'−{saved}', _GREEN, _BOLD)}"

        print(
            f"{self._label('CONTEXT', _BLUE)}"
            f"{_c(total_bar, _CYAN)} {total_tokens}/{max_tokens} tok ({pct:.0f}%)  "
            f"{compress_str}"
        )

        ref = max(total_tokens, 1)
        layers = [
            ("sys", l0, _BLUE),
            ("state", l1, _DIM),
            ("mem", l2, _MAGENTA),
            ("hist", l3, _GREEN),
        ]
        parts = []
        for name, val, color in layers:
            b = _c(self._bar(val / ref), color)
            parts.append(f"{name} {b} {val}")
        print(f"{'':>16}{'  '.join(parts)}")

    def _memory(self, *, query: str = "", hits: int = 0, **_: Any) -> None:
        status = _c(f"{hits} hits", _GREEN if hits else _DIM)
        print(f"{self._label('MEMORY', _BLUE)}Recalled {status} for {self._dim(repr(query))}")

    def _memory_classify(
        self, *, scanned: int = 0, written: int = 0, types: str = "", **_: Any
    ) -> None:
        detail = _c(f"wrote {written} ({types})", _GREEN) if written else _c("wrote 0", _DIM)
        print(f"{self._label('MEMORY', _BLUE)}Classify scanned {scanned} segment(s), {detail}")

    def _learning_synthesize(
        self, *, action: str = "", name: str = "", detail: str = "", **_: Any
    ) -> None:
        if action == "patched":
            print(
                f"{self._label('LEARNING', _MAGENTA)}"
                f"Skill {_c(repr(name), _BOLD)} patched  {self._dim(detail)}"
            )
        else:
            print(
                f"{self._label('LEARNING', _MAGENTA)}"
                f"{_c('Skill synthesis skipped', _DIM)}  {self._dim(detail[:120])}"
            )

    def _injection_failed(
        self, *, point: str = "", injector: str = "", error: str = "", **_: Any
    ) -> None:
        print(
            f"{self._label('INJECT', _RED)}"
            f"{_c('FAILED', _RED, _BOLD)} at {_c(point, _BOLD)}  "
            f"{self._dim(injector)}  {self._dim(error[:120])}"
        )

    def _skills(self, *, count: int = 0, names: list[str] | None = None, **_: Any) -> None:
        names = names or []
        print(f"{self._label('SKILLS', _BLUE)}{count} runbooks: {self._dim(', '.join(names))}")

    def _turn_start(self, *, turn: int, max_turns: int = 0, run_id: str = "", **_: Any) -> None:
        self._turn = turn
        self._think_started = False
        # Surface sub-agent name so parallel turns don't look like one counter.
        suffix = _subagent_suffix(run_id)
        bar = f"{'─' * 4} Turn {turn}"
        if max_turns:
            bar += f"/{max_turns}"
        bar += suffix
        print(f"\n{_c(bar + ' ' + '─' * max(0, 68 - len(bar)), _DIM)}")

    def _think(self, *, text: str = "", **_: Any) -> None:
        if not text:
            return
        if not self._think_started:
            print(f"{self._label('THINK', _MAGENTA)}", end="", flush=True)
            self._think_started = True
        print(text, end="", flush=True)

    def _llm_request(
        self,
        *,
        phase: str = "",
        system: str = "",
        system_len: int = 0,
        msg_count: int = 0,
        **_: Any,
    ) -> None:
        # Without this banner, terminal jumps from SESSION to PLAN with no LLM signal.
        if not phase:
            return
        label = "PLANNING" if phase == "planning" else "LLM CALL"
        color = _YELLOW if phase == "planning" else _CYAN
        slen = system_len or len(system)
        print(
            f"{self._label(label, color)}"
            f"calling LLM  {self._dim(f'({msg_count} msgs, system≈{slen} chars)')}"
        )

    def _tool_call(
        self,
        *,
        name: str = "",
        params: dict[str, Any] | None = None,
        side_effect_level: str = "",
        readonly: bool = True,
        approval_required: bool = False,
        **_: Any,
    ) -> None:
        params = params or {}
        if self._think_started:
            print()
            self._think_started = False

        level_str = side_effect_level or "low"
        level_color = {
            "low": _GREEN,
            "medium": _YELLOW,
            "high": _RED,
        }.get(level_str.lower(), _CYAN)
        kind = "readonly" if readonly else "write"
        level_display = _c(f"{kind} {level_str.upper()}", level_color, _BOLD)

        name_field = _c(name, _BOLD)
        sep = _c("─" * max(1, 48 - len(name) - len(kind) - len(level_str) - 2), _DIM)
        print(f"\n{self._label('TOOL CALL', _CYAN)}{name_field} {sep} {level_display}")

        if self._verbose:
            for k, v in params.items():
                print(f"               {self._dim(k)}={repr(v)[:60]}")

        if approval_required:
            print(f"               {_c('approval_required=True', _RED, _BOLD)}")

    def _approval_request(
        self, *, name: str = "", params: dict[str, Any] | None = None, level: str = "", **_: Any
    ) -> None:
        params = params or {}
        w = 66
        print(f"\n  {_c('╔' + '═' * w + '╗', _RED)}")
        print(f"  {_c('║', _RED)} {_c('APPROVAL REQUEST', _RED, _BOLD):<{w - 1}}{_c('║', _RED)}")
        print(f"  {_c('║', _RED)} {'Tool:   ' + name:<{w - 1}}{_c('║', _RED)}")
        for k, v in params.items():
            val_str = repr(v)[: w - 11]
            print(f"  {_c('║', _RED)} {f'{k}: {val_str}':<{w - 1}}{_c('║', _RED)}")
        print(
            f"  {_c('║', _RED)} {'Risk:   HIGH side-effect — irreversible action':<{w - 1}}{_c('║', _RED)}"
        )
        print(f"  {_c('║', _RED)} {'[a] Approve   [d] Deny':<{w - 1}}{_c('║', _RED)}")
        print(f"  {_c('╚' + '═' * w + '╝', _RED)}")

    def _tool_result(
        self,
        *,
        name: str = "",
        result: dict[str, Any] | None = None,
        elapsed_ms: float = 0,
        **_: Any,
    ) -> None:
        result = result or {}
        summary = str(result)[:80]
        elapsed = _c(f"[{elapsed_ms:.0f}ms]", _DIM)
        arrow = _c("←", _GREEN)
        print(f"  {arrow} {_c(name, _BOLD):<28} {self._dim(summary)}  {elapsed}")

    def _plan_ready(
        self,
        *,
        plan_id: str = "",
        version: int = 1,
        steps: list[dict[str, Any]] | None = None,
        agent: str = "",
        **_: Any,
    ) -> None:
        steps = steps or []
        step_desc = " → ".join(f"{s.get('id', '?')}:{s.get('action', '?')}" for s in steps[:4])
        print(
            f"\n{self._label('PLAN', _YELLOW)}"
            f"Agent={_c(repr(agent), _BOLD)} v{version}  "
            f"{_c(f'{len(steps)} steps', _BOLD)}: {self._dim(step_desc)}"
        )
        for s in steps:
            self._print_step_detail(s)

    def _plan_replanned(
        self,
        *,
        plan_id: str = "",
        version: int = 1,
        failed_step: str = "",
        new_steps: list[dict[str, Any]] | None = None,
        replan_count: int = 1,
        **_: Any,
    ) -> None:
        new_steps = new_steps or []
        ids_str = ", ".join(s.get("id", "?") for s in new_steps)
        print(
            f"\n{self._label('REPLAN', _YELLOW)}"
            f"#{replan_count} v{version}  failed={_c(repr(failed_step), _RED)}  "
            f"new={_c(repr(ids_str), _BOLD)}"
        )
        for s in new_steps:
            self._print_step_detail(s)

    def _print_step_detail(self, step: dict[str, Any]) -> None:
        sid = step.get("id", "?")
        action = step.get("action", "?")
        params = step.get("params") or {}
        depends_on = step.get("depends_on") or []
        indent = "               "
        dep_str = ", ".join(depends_on) if depends_on else "—"
        print(
            f"{indent}{_c(sid, _BOLD)}:{_c(action, _CYAN)}  "
            f"{self._dim('depends_on=[' + dep_str + ']')}"
        )
        if params:
            try:
                params_str = json.dumps(params, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                params_str = repr(params)
            print(f"{indent}  params: {self._dim(params_str)}")

    def _step_started(self, *, step_id: str = "", action: str = "", **_: Any) -> None:
        print(f"{self._label('STEP', _YELLOW)}▶ {_c(step_id, _BOLD)}:{_c(action, _CYAN)}")

    def _step_completed(
        self, *, step_id: str = "", action: str = "", result: Any = None, **_: Any
    ) -> None:
        summary = str(result)[:80] if result is not None else ""
        print(
            f"{self._label('STEP', _GREEN)}✓ {_c(step_id, _BOLD)}:{_c(action, _CYAN)}  "
            f"{self._dim(summary)}"
        )

    def _step_failed(
        self, *, step_id: str = "", action: str = "", error: str = "", **_: Any
    ) -> None:
        print(
            f"{self._label('STEP', _RED)}✗ {_c(step_id, _BOLD)}:{_c(action, _CYAN)}  "
            f"{_c(error[:120], _RED)}"
        )

    def _skill_load(self, *, name: str = "", chars: int = 0, path: str = "", **_: Any) -> None:
        print(
            f"{self._label('SKILL', _MAGENTA)}Loaded {_c(repr(name), _BOLD)} "
            f"({chars} chars)  {self._dim(path)}"
        )

    def _token_update(
        self,
        *,
        turn: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0,
        budget_usd: float = 0,
        max_turns: int = 0,
        elapsed_s: float = 0,
        max_seconds: float = 0,
        run_id: str = "",
        **_: Any,
    ) -> None:
        total = input_tokens + output_tokens
        pct = (cost_usd / budget_usd * 100) if budget_usd else 0
        turns_str = f"{turn}/{max_turns}" if max_turns else str(turn)
        time_str = f"{elapsed_s:.1f}s/{max_seconds:.0f}s" if max_seconds else f"{elapsed_s:.1f}s"
        suffix = _subagent_suffix(run_id)
        print(
            f"\n{self._label('BUDGET', _DIM)}"
            f"Turn {turns_str}{suffix} | Tokens +{output_tokens} (Σ{total}) | "
            f"${cost_usd:.4f}/${budget_usd:.2f} ({pct:.1f}%) | {time_str}"
        )

    def _agent_spawn(
        self, *, name: str = "", task: str = "", packet_id: str = "", **_: Any
    ) -> None:
        packet_str = f"  {self._dim(f'packet={packet_id[:8]}')}" if packet_id else ""
        task_preview = task[:120] + (" …" if len(task) > 120 else "")
        bar = _c("─" * 40, _DIM)
        print(f"\n{bar}")
        print(
            f"{self._label('SUB-AGENT', _MAGENTA)}"
            f"Spawning {_c(name[:16], _BOLD)}{packet_str}  "
            f"task={self._dim(repr(task_preview))}"
        )

    def _agent_result(self, *, name: str = "", state: str = "", turns: int = 0, **_: Any) -> None:
        color = _GREEN if state == "completed" else _RED
        bar = _c("─" * 40, _DIM)
        print(
            f"{self._label('SUB-AGENT', _MAGENTA)}"
            f"{_c(name[:16], _BOLD)} → {_c(state.upper(), color)}  {turns} turns"
        )
        print(bar)

    def _run_complete(
        self,
        *,
        run_id: str = "",
        state: str = "",
        turns: int = 0,
        total_tokens: int = 0,
        cost_usd: float = 0,
        elapsed_s: float = 0,
        **_: Any,
    ) -> None:
        # Only top-level run gets the full bar; sub-agents rendered elsewhere.
        if is_subagent_run_id(run_id):
            return
        color = _GREEN if state == "completed" else _RED
        bar = "═" * 72
        print(f"\n{_c(bar, color)}")
        status = _c(f"  {state.upper()}", color, _BOLD)
        stats = f"  {turns} turns | {total_tokens:,} tokens | ${cost_usd:.4f} | {elapsed_s:.1f}s"
        print(f"{status}{_c(stats, _DIM)}")
        print(_c(bar, color))

    def _run_failed(
        self,
        *,
        run_id: str = "",
        error: str = "",
        turns: int = 0,
        cost_usd: float = 0,
        **_: Any,
    ) -> None:
        if is_subagent_run_id(run_id):
            return
        print(
            f"{self._label('RUN', _RED)}✗ FAILED after {turns} turns "
            f"(${cost_usd:.4f})  {self._dim(error[:120])}"
        )

    def _checkpoint_failed(self, *, run_id: str = "", turns: int = 0, **_: Any) -> None:
        print(
            f"{self._label('CHECKPOINT', _YELLOW)}! save failed for run="
            f"{_c(run_id[:16], _BOLD)} at turn {turns} — durable state may be stale"
        )

    def _peer_handoff(
        self,
        *,
        from_agent: str = "",
        to: str = "",
        task: str = "",
        depth: int = 0,
        **_: Any,
    ) -> None:
        print(
            f"{self._label('HANDOFF', _MAGENTA)}{_c(from_agent[:16], _BOLD)} → "
            f"{_c(to[:16], _BOLD)}  {self._dim('depth=' + str(depth))}  "
            f"{self._dim(task[:60])}"
        )

    def _loop_end(self, *, run_id: str = "", error: str | None = None, **_: Any) -> None:
        if not error:
            return
        print(f"{self._label('LOOP', _RED)}✗ ended with error  {self._dim(str(error)[:120])}")


# Dispatch table — HookEvent → bound method name.
_HANDLERS: dict[HookEvent, str] = {
    HookEvent.SESSION_START: "_session_start",
    HookEvent.SESSION_END: "_session_end",
    HookEvent.CONTEXT_BUILD: "_context",
    HookEvent.MEMORY_RECALL: "_memory",
    HookEvent.MEMORY_CLASSIFY: "_memory_classify",
    HookEvent.INJECTION_FAILED: "_injection_failed",
    HookEvent.SKILLS_READY: "_skills",
    HookEvent.TURN_START: "_turn_start",
    HookEvent.LLM_REQUEST: "_llm_request",
    HookEvent.THINK: "_think",
    HookEvent.TOOL_CALL: "_tool_call",
    HookEvent.APPROVAL_REQUEST: "_approval_request",
    HookEvent.TOOL_RESULT: "_tool_result",
    HookEvent.PLAN_READY: "_plan_ready",
    HookEvent.PLAN_REPLANNED: "_plan_replanned",
    HookEvent.STEP_STARTED: "_step_started",
    HookEvent.STEP_COMPLETED: "_step_completed",
    HookEvent.STEP_FAILED: "_step_failed",
    HookEvent.SKILL_LOAD: "_skill_load",
    HookEvent.TOKEN_UPDATE: "_token_update",
    HookEvent.AGENT_SPAWN: "_agent_spawn",
    HookEvent.AGENT_RESULT: "_agent_result",
    HookEvent.RUN_COMPLETE: "_run_complete",
    HookEvent.RUN_FAILED: "_run_failed",
    HookEvent.CHECKPOINT_FAILED: "_checkpoint_failed",
    HookEvent.PEER_HANDOFF: "_peer_handoff",
    HookEvent.LOOP_END: "_loop_end",
    HookEvent.LEARNING_SYNTHESIZE: "_learning_synthesize",
    # LOOP_START is deliberately not rendered — it would double-print with
    # SESSION_START on every hop.
}


__all__ = ["ConsoleObserverHooks"]
