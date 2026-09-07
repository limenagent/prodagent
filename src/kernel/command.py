"""command — the only control vocabulary a node uses to talk to the scheduler.

When a node finishes, its Outcome may carry one control command telling the
scheduler how the next wave should go. The command only describes intent; the
scheduler is what actually changes the ready set. A node never mutates another
node's state directly — control is centralized in the engine.

There are only two control commands, no hidden vocabulary:

- no command (None): follow the static edges declared in the Plan;
- Goto: re-arm a node, used for loops/back-edges (this is how ReAct cycles)
  and runtime jumps; it may carry a payload as the target's next input;
- Send: instantiate a template node at runtime, used for fan-out when the
  number of branches is only known while running.

Multi-agent "transfer" is not a new command: a Goto to another agent node in
the same graph, with no return edge, means "hand over and don't come back";
the handover summary travels in the Goto payload.

Why is there no Update command? Writing data into state is already the job of
Outcome.state_delta, a first-class field. Data is data, control is control.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Command:
    """Base class for control commands; frozen makes each an immutable fact."""


@dataclass(frozen=True)
class Goto(Command):
    """Re-arm ``target`` (move it back to PENDING) so it can run again.

    Once re-armed, *when* it becomes ready has two modes, picked by
    ``immediate``:
    - immediate=True (default): also add it to the release list, so next wave
      it bypasses its predecessors and becomes ready immediately. Used for
      sequential back-edges (the ReAct loop) and runtime jumps.
    - immediate=False: only re-arm, do not release yet; readiness is still
      decided by incoming edges and join. Used at iterative convergence points
      (multi-round blackboard, synthesis after re-planning) — wait for this
      wave's predecessors to finish again, instead of racing them and reading
      empty results in the same wave.

    ``payload`` is the input handed to the target on this transition, symmetric
    with Send.payload: static edges pass values via upstream output, while
    dynamic transitions pass values via command payload (multi-agent handover
    uses it to carry the summary).

    ``Goto.now``/``Goto.rejoin`` are named constructors for the two modes
    above; they change nothing but how the call site reads.
    """

    target: str
    immediate: bool = True
    payload: Any = None

    @classmethod
    def now(cls, target: str, payload: Any = None) -> Goto:
        """Re-arm and release immediately (back-edge/jump/handover)."""
        return cls(target, immediate=True, payload=payload)

    @classmethod
    def rejoin(cls, target: str, payload: Any = None) -> Goto:
        """Re-arm only; wait for this wave's predecessors/join like any other edge."""
        return cls(target, immediate=False, payload=payload)


@dataclass(frozen=True)
class Send(Command):
    """Dynamic fan-out: instantiate the template node once and feed it payload.

    ``key`` is the instance identity; when absent the scheduler numbers them
    in order. The same key is used to de-duplicate.
    """

    template: str
    payload: Any = None
    key: str | None = None
