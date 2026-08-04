"""Trajectory-level test assertions (ch18 — the third lock)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from prodagent.core.state.run import AgentRun


class TrajectoryCheck(Protocol):
    """One queued assertion over a run's tool-call trajectory."""

    def check(self, ctx: _CheckContext) -> str | None:
        """Return ``None`` on pass, or a diagnostic message on failure."""
        ...


@dataclass
class _CheckContext:
    """Resolved trajectory state passed to each check during ``assert_all``."""

    run: AgentRun
    sequence: list[str]
    last_named: str | None = None


def _format_sequence(seq: list[str]) -> str:
    return f"Actual sequence: {seq}"


@dataclass
class _Called:
    tool: str

    def check(self, ctx: _CheckContext) -> str | None:
        if self.tool not in ctx.sequence:
            return f"Expected '{self.tool}' to be called but it never was.\n" + _format_sequence(
                ctx.sequence
            )
        ctx.last_named = self.tool
        return None


@dataclass
class _Never:
    tool: str

    def check(self, ctx: _CheckContext) -> str | None:
        if self.tool in ctx.sequence:
            return (
                f"Tool '{self.tool}' was called but should never have been.\n"
                + _format_sequence(ctx.sequence)
            )
        return None


@dataclass
class _AfterPrevious:
    tool: str

    def check(self, ctx: _CheckContext) -> str | None:
        if ctx.last_named is not None:
            i = ctx.sequence.index(ctx.last_named) if ctx.last_named in ctx.sequence else -1
            if i < 0 or self.tool not in ctx.sequence[i + 1 :]:
                return (
                    f"Expected '{self.tool}' to be called after '{ctx.last_named}'.\n"
                    + _format_sequence(ctx.sequence)
                )
        ctx.last_named = self.tool
        return None


@dataclass
class _MaxTurns:
    n: int

    def check(self, ctx: _CheckContext) -> str | None:
        if ctx.run.turn_count > self.n:
            return f"Run took {ctx.run.turn_count} turns but max was {self.n}"
        return None


@dataclass
class _MinTurns:
    n: int

    def check(self, ctx: _CheckContext) -> str | None:
        if ctx.run.turn_count < self.n:
            return f"Run took {ctx.run.turn_count} turns but min was {self.n}"
        return None


@dataclass
class _Count:
    tool: str
    exactly: int

    def check(self, ctx: _CheckContext) -> str | None:
        actual = ctx.sequence.count(self.tool)
        if actual != self.exactly:
            return (
                f"Tool '{self.tool}' called {actual} times, expected exactly {self.exactly}.\n"
                + _format_sequence(ctx.sequence)
            )
        return None


@dataclass
class _NoRepeat:
    tool: str

    def check(self, ctx: _CheckContext) -> str | None:
        indices = [i for i, t in enumerate(ctx.sequence) if t == self.tool]
        for a, b in zip(indices, indices[1:], strict=False):
            if b - a == 1:
                return (
                    f"Tool '{self.tool}' called consecutively at positions {a} and {b} "
                    f"— possible dead loop.\n" + _format_sequence(ctx.sequence)
                )
        return None


@dataclass
class _CalledWith:
    tool: str
    expected: dict[str, Any] = field(default_factory=dict)

    def check(self, ctx: _CheckContext) -> str | None:
        for tc in ctx.run.tool_history:
            if tc.name != self.tool:
                continue
            if all(tc.params.get(k) == v for k, v in self.expected.items()):
                ctx.last_named = self.tool
                return None
        return (
            f"Tool '{self.tool}' never called with params {self.expected}.\n"
            f"Actual tool_history: "
            f"{[(tc.name, tc.params) for tc in ctx.run.tool_history]}"
        )


class TrajectoryAssert:
    """Fluent assertion builder over an agent run's tool-call trajectory."""

    def __init__(self, run: AgentRun) -> None:
        self._run = run
        self._checks: list[TrajectoryCheck] = []

    def called(self, tool_name: str) -> TrajectoryAssert:
        self._checks.append(_Called(tool=tool_name))
        return self

    def called_with(self, tool_name: str, **expected_params: Any) -> TrajectoryAssert:
        self._checks.append(_CalledWith(tool=tool_name, expected=dict(expected_params)))
        return self

    def then(self, tool_name: str) -> TrajectoryAssert:
        self._checks.append(_AfterPrevious(tool=tool_name))
        return self

    def never_called(self, tool_name: str) -> TrajectoryAssert:
        self._checks.append(_Never(tool=tool_name))
        return self

    def max_turns(self, n: int) -> TrajectoryAssert:
        self._checks.append(_MaxTurns(n=n))
        return self

    def min_turns(self, n: int) -> TrajectoryAssert:
        self._checks.append(_MinTurns(n=n))
        return self

    def call_count(self, tool_name: str, exactly: int) -> TrajectoryAssert:
        self._checks.append(_Count(tool=tool_name, exactly=exactly))
        return self

    def no_repeated_calls(self, tool_name: str) -> TrajectoryAssert:
        self._checks.append(_NoRepeat(tool=tool_name))
        return self

    def assert_all(self) -> None:
        ctx = _CheckContext(run=self._run, sequence=self._sequence())
        for check in self._checks:
            msg = check.check(ctx)
            if msg is not None:
                raise AssertionError(msg)

    def _sequence(self) -> list[str]:
        return [tc.name for tc in self._run.tool_history]


__all__ = ["TrajectoryAssert", "TrajectoryCheck"]
