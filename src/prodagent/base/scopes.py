"""Scopes — the five-level state addressing of column 9.

Who can see a piece of state, and for how long it lives, is a *scope*
question, orthogonal to what the value is. The column's ladder:

    app      shared across the whole application (public knowledge, config)
    user     one user, across sessions (preferences, long-term profile)
    session  one conversation, across runs (the dialogue spine)
    run      one execution, private (this run's intermediates)
    temp     in-process scratch, never persisted, discarded on exit

The judgment rule for which side a value belongs on: after a crash, must it
be *restored verbatim* from the snapshot (State — run/session/user/app) or
*re-supplied fresh* by the new environment (Context, temp)? Identity is
stored as a serializable value; live handles are rebuilt — never the other
way around.

:interfaces:`ScopeView` is the controlled door: a run reads outward freely
(session/user/app) but writes only its own ``run`` layer unless a scope was
explicitly granted — a shared layer must not be polluted by any single
run's casual write (concurrent isolation, multi-tenant safety). ``temp``
rides beside the ladder: process-local, never serialized, use-and-discard.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = ["Scope", "ScopeError", "ScopeView"]


class Scope(StrEnum):
    """The five state scopes, outermost to the private run layer."""

    APP = "app"
    USER = "user"
    SESSION = "session"
    RUN = "run"
    TEMP = "temp"


class ScopeError(PermissionError):
    """A write to a scope the run was not granted."""

    def __init__(self, scope: Scope) -> None:
        super().__init__(
            f"scope {scope!r} is read-only here — writing beyond the run "
            "layer needs an explicit grant (a shared layer is not a "
            "scratchpad for any single run)"
        )


class ScopeView:
    """A run's controlled window onto the scope ladder.

    Reads fall through inner-to-outer (run → session → user → app): the
    first layer holding the key answers, mirroring lexical scoping. Writes
    land in the run layer by default; every outer scope must appear in
    ``writable`` — granted deliberately, never by default.
    """

    def __init__(
        self,
        *,
        run: dict[str, Any] | None = None,
        session: dict[str, Any] | None = None,
        user: dict[str, Any] | None = None,
        app: dict[str, Any] | None = None,
        temp: dict[str, Any] | None = None,
        writable: frozenset[Scope] = frozenset({Scope.RUN, Scope.TEMP}),
    ) -> None:
        self._layers: dict[Scope, dict[str, Any] | None] = {
            Scope.RUN: run,
            Scope.SESSION: session,
            Scope.USER: user,
            Scope.APP: app,
        }
        self._temp = temp
        self._writable = writable

    def get(self, key: str, default: Any = None, *, scope: Scope | None = None) -> Any:
        """Read one key — from a named scope, or falling through the ladder."""
        if scope is not None:
            layer = self._temp if scope is Scope.TEMP else self._layers.get(scope)
            return default if layer is None else layer.get(key, default)
        if (run := self._layers[Scope.RUN]) is not None and key in run:
            return run[key]
        for outer in (Scope.SESSION, Scope.USER, Scope.APP):
            layer = self._layers[outer]
            if layer is not None and key in layer:
                return layer[key]
        if self._temp is not None and key in self._temp:
            return self._temp[key]
        return default

    def put(self, key: str, value: Any, *, scope: Scope = Scope.RUN) -> None:
        """Write one key — the run (and temp) layers by default, outer
        layers only when granted (fail closed on an ungranted scope)."""
        if scope not in self._writable:
            raise ScopeError(scope)
        layer = self._temp if scope is Scope.TEMP else self._layers.get(scope)
        if layer is None:
            raise LookupError(f"scope {scope!r} is not mounted on this view")
        layer[key] = value
