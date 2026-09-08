"""run — one dynamic execution: node runtime states, the Interrupt token, the
Run state machine, and snapshots.

The Plan is the drawing; a Run is one execution of it. It holds current shared
state, how far each node got, parent/child relations, and the four states
RUNNING/SUSPENDED/COMPLETED/FAILED. State transitions have a single entry
point _transition; an illegal jump raises immediately.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from src.kernel.channels import Channel
from src.kernel.types import _ALLOWED_TRANSITIONS, NodeStatus, RunState


def _new_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@dataclass
class NodeRuntimeState:
    """Runtime state of a node (or dynamic instance) within this Run."""

    status: NodeStatus = NodeStatus.PENDING
    output: Any = None
    attempts: int = 0

    def mark_running(self) -> None:
        self.status = NodeStatus.RUNNING
        self.attempts += 1

    def mark_completed(self, output: Any) -> None:
        self.status = NodeStatus.COMPLETED
        self.output = output

    def mark_skipped(self) -> None:
        self.status = NodeStatus.SKIPPED

    def mark_failed(self, error: str) -> None:
        self.status = NodeStatus.FAILED
        self.output = error

    def reset_pending(self) -> None:
        """On a Goto back-edge, return the node to pending for the next wave."""
        self.status = NodeStatus.PENDING


@dataclass(frozen=True)
class Interrupt:
    """A suspension token: at which node, why, and what to ask the outside world."""

    kind: str  # approval / input / external
    payload: Any = None
    question: str = ""
    node_id: str = ""  # filled in by Run when parking


class Run:
    """One execution instance of a Plan. The same Plan can have many Runs at
    once that never interfere with each other."""

    def __init__(
        self,
        plan: Any,
        run_id: str | None = None,
        *,
        parent_id: str | None = None,
        depth: int = 0,
        task: str = "",
    ):
        self.plan = plan
        self.run_id = run_id or _new_id()
        self.parent_id = parent_id
        self.depth = depth
        self.task = task
        self.event_seq = 0

        self.shared: dict[str, Any] = plan.initial_shared()
        self.node_states: dict[str, NodeRuntimeState] = {
            nid: NodeRuntimeState() for nid in plan.nodes
        }
        # Dynamic fan-out: template id -> list of instance keys it produced;
        # instance inputs are stored separately.
        self.instances: dict[str, list[str]] = {}
        self.instance_inputs: dict[str, Any] = {}
        # Input handed to a target on a Goto transition (dynamic-edge value
        # passing, symmetric with Send.payload).
        self.deliveries: dict[str, Any] = {}
        self._instance_seq = 0
        # Nodes explicitly activated by a control command (Goto). Under an
        # explicit entry, only the entry and these nodes can start "without an
        # incoming edge", so an isolated node isn't mistaken for a source on the
        # first wave.
        self.activated: set[str] = set()

        self.state: RunState = RunState.RUNNING
        self.interrupts: dict[str, Interrupt] = {}  # node id -> why it parked
        self.resume_values: dict[str, Any] = {}  # node id -> external value fed back on resume
        self.final_output: Any = None
        self.metrics: dict[str, int] = {"waves": 0, "llm_calls": 0, "tool_calls": 0}

    @property
    def name(self) -> str:
        """The blueprint's name — static identity derived from the plan, never
        run state: it does not change per execution and never enters a
        snapshot; restore(plan, snap) always has the plan at hand."""
        return self.plan.name

    # — convenient construction —
    @classmethod
    def start(cls, plan: Any, **kw: Any) -> Run:
        plan.validate()
        return cls(plan, **kw)

    # — state machine: the single transition entry —
    def _transition(self, target: RunState) -> None:
        allowed = _ALLOWED_TRANSITIONS[self.state]
        if target not in allowed:
            raise RuntimeError(f"illegal state transition: {self.state} -> {target}")
        self.state = target

    def complete(self, output: Any = None) -> None:
        self.final_output = output
        self._transition(RunState.COMPLETED)

    def fail(self, reason: str) -> None:
        self.final_output = reason
        self._transition(RunState.FAILED)

    def suspend(self, interrupts: dict[str, Interrupt]) -> None:
        """Park as a whole: every node that asked to park this wave, keyed by node id."""
        self.interrupts = interrupts
        self._transition(RunState.SUSPENDED)

    def resume(self, values: dict[str, Any] | None = None) -> None:
        """Feed back one external value per parked node id."""
        self.resume_values = dict(values or {})
        self.interrupts = {}
        self._transition(RunState.RUNNING)

    def take_resume(self, key: str) -> Any:
        return self.resume_values.pop(key, None)

    @property
    def running(self) -> bool:
        return self.state == RunState.RUNNING

    # — node state queries —
    def state_of(self, key: str) -> NodeRuntimeState:
        return self.node_states[key]

    @staticmethod
    def is_instance_key(key: str) -> bool:
        return "#" in key

    def is_instance(self, key: str) -> bool:
        return self.is_instance_key(key) and key in self.instance_inputs

    def template_of(self, key: str) -> str:
        return key.split("#", 1)[0] if self.is_instance_key(key) else key

    def is_pending(self, key: str) -> bool:
        st = self.node_states.get(key)
        return st is not None and st.status == NodeStatus.PENDING

    def is_completed(self, key: str) -> bool:
        st = self.node_states.get(key)
        return st is not None and st.status == NodeStatus.COMPLETED

    def is_terminal(self, key: str) -> bool:
        st = self.node_states.get(key)
        return st is not None and st.status in (
            NodeStatus.COMPLETED,
            NodeStatus.SKIPPED,
            NodeStatus.FAILED,
        )

    # — node state changes —
    def mark_running(self, key: str) -> None:
        self.node_states[key].mark_running()
        # Consume the immediate-activation pass now: without this, a node
        # that was ever Goto(immediate=True)'d would stay in `activated`
        # forever, bypassing predecessor/join gating on every future rearm.
        self.activated.discard(key)

    def mark_completed(self, key: str, output: Any) -> None:
        self.node_states[key].mark_completed(output)

    def mark_skipped(self, key: str) -> None:
        self.node_states[key].mark_skipped()

    def mark_failed(self, key: str, error: str) -> None:
        self.node_states[key].mark_failed(error)

    def rearm(self, key: str) -> None:
        """Re-arm: move a (possibly COMPLETED) node back to PENDING.

        This is the one low-level action that lets a node run again. It only
        changes state; it does not decide whether to release it immediately.
        """
        self.node_states.setdefault(key, NodeRuntimeState()).reset_pending()

    def reset_pending(self, key: str) -> None:
        """Used by Goto: re-arm and add to activated — next wave it bypasses
        predecessors and becomes ready immediately."""
        self.rearm(key)
        self.activated.add(key)  # record "command-activated now" for the readiness check

    # — dynamic instances (Send fan-out) —
    def add_instance(self, template: str, payload: Any, key: str | None = None) -> str:
        self._instance_seq += 1
        # Internal keys carry a "template#" prefix, so a key always reveals which
        # template it is an instance of.
        suffix = key if key is not None else str(self._instance_seq)
        full_key = f"{template}#{suffix}"
        if full_key not in self.node_states:
            self.node_states[full_key] = NodeRuntimeState()
            self.instance_inputs[full_key] = payload
            self.instances.setdefault(template, []).append(full_key)
        return full_key

    def input_of(self, key: str) -> Any:
        # A dynamic instance consumes the payload carried by Send; a static node
        # consumes the previous step's output (passed separately by the scheduler).
        return self.instance_inputs.get(key)

    # — wave barrier: fold this wave's deltas with channel reducers —
    def fold_writes(self, writes: list[Any], channels: dict[str, Channel]) -> dict[str, Any]:
        """Fold this wave's deltas into shared state and return the "wave delta"
        (for the event log to record).

        Two steps, in this order: first aggregate the multiple writes to a
        channel within the wave from the identity element into one wave delta,
        then fold that wave delta into historical state. This way an event
        stores "what this wave added", and replaying the whole stream wave by
        wave rebuilds the final state without double-counting.
        """
        wave_delta: dict[str, Any] = {}
        for w in writes:
            channel = channels[w.key]
            base = wave_delta.get(w.key, channel.empty)
            wave_delta[w.key] = channel.fold(base, w.value)
        for key, delta in wave_delta.items():
            channel = channels[key]
            self.shared[key] = channel.fold(self.shared.get(key, channel.init), delta)
        return wave_delta

    # — snapshot and restore: store only data, not the blueprint or live ports —
    def snapshot(self) -> dict[str, Any]:
        # A snapshot is detached history: every mutable container is copied
        # here (shallow is enough — reducers never mutate channel values in
        # place), so a run's later writes cannot silently rewrite what a
        # checkpoint store already holds. restore() copies again on its side
        # for the same reason: the resumed run must not alias the store's copy.
        return {
            "run_id": self.run_id,
            "parent_id": self.parent_id,
            "depth": self.depth,
            "task": self.task,
            "state": str(self.state),
            "shared": dict(self.shared),
            "node_states": {
                k: {"status": str(v.status), "output": v.output, "attempts": v.attempts}
                for k, v in self.node_states.items()
            },
            "instances": {k: list(v) for k, v in self.instances.items()},
            "instance_inputs": dict(self.instance_inputs),
            "deliveries": dict(self.deliveries),
            "instance_seq": self._instance_seq,
            "activated": list(self.activated),
            "interrupts": {k: v.__dict__ for k, v in self.interrupts.items()},
            "final_output": self.final_output,
            "metrics": dict(self.metrics),
            "event_seq": self.event_seq,
        }

    @classmethod
    def restore(cls, plan: Any, snap: dict[str, Any]) -> Run:
        run = cls(
            plan,
            run_id=snap["run_id"],
            parent_id=snap.get("parent_id"),
            depth=snap.get("depth", 0),
            task=snap.get("task", ""),
        )
        run.shared = dict(snap["shared"])
        run.node_states = {
            k: NodeRuntimeState(NodeStatus(v["status"]), v.get("output"), v.get("attempts", 0))
            for k, v in snap["node_states"].items()
        }
        run.instances = {k: list(v) for k, v in snap.get("instances", {}).items()}
        run.instance_inputs = dict(snap.get("instance_inputs", {}))
        run.deliveries = dict(snap.get("deliveries", {}))
        run._instance_seq = snap.get("instance_seq", 0)
        run.activated = set(snap.get("activated", ()))
        run.final_output = snap.get("final_output")
        run.metrics = dict(snap.get("metrics", run.metrics))
        run.event_seq = snap.get("event_seq", 0)
        # Restore lands directly on the saved state, bypassing construction-time RUNNING.
        run.state = RunState(snap["state"])
        run.interrupts = {
            k: Interrupt(d["kind"], d.get("payload"), d.get("question", ""), d.get("node_id", ""))
            for k, d in snap.get("interrupts", {}).items()
        }
        return run
