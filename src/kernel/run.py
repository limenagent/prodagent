"""run — one dynamic execution: node runtime states, the Interrupt token, the
Run state machine, and apply — the single mutation throat.

The Plan is the drawing; a Run is one execution of it. It holds current shared
state, how far each node got, parent/child relations, and the four states
RUNNING/SUSPENDED/COMPLETED/FAILED. State transitions have a single entry
point _transition; an illegal jump raises immediately. Ledger state changes
only by applying a fact (Run.apply) — never by discipline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from src.kernel.channels import Channel
from src.kernel.eventlog import (
    CONTROL,
    INTERRUPTED,
    NODE_COMPLETED,
    NODE_FAILED,
    NODE_SKIPPED,
    NODE_STARTED,
    RESUMED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    STATE_DELTA,
    Event,
    apply_event,
)
from src.kernel.types import _ALLOWED_TRANSITIONS, NodeStatus, RunState


def _new_id() -> str:
    return f"run-{uuid.uuid4().hex[:8]}"


def _is_instance_key(key: str) -> bool:
    # dynamic fan-out instances are named "template#key"
    return "#" in key


# Safety backstop, not a tuning knob: performance limits (max_waves,
# concurrency) are per-Scheduler params; this one guards tree structure and is
# pinned. 8 keeps the old max_depth default — Claude Code pins subagent depth
# at 3, and most frameworks ship no structural cap at all.
_MAX_RUN_DEPTH = 8


@dataclass
class NodeRuntimeState:
    """Runtime state of a node (or dynamic instance) within this Run: a pure
    record — every change goes through Run's mark_*/rearm methods."""

    status: NodeStatus = NodeStatus.PENDING
    output: Any = None
    attempts: int = 0  # executions started; NODE_RETRY events count their own index


@dataclass(frozen=True)
class Interrupt:
    """A suspension token: at which node, why, and what to ask the outside world."""

    kind: str  # approval / input / external / delegation
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
        seed: dict[str, Any] | None = None,
        llm: Any = None,
        tools: Any = None,
    ):
        self.plan = plan
        self.run_id = run_id or _new_id()
        self.parent_id = parent_id
        self.depth = depth
        self.task = task
        # Port binding = *who* this Run acts as (an Agent's model/tools). It is
        # live wiring, like NodeContext: never a fact. The shared ledger (event
        # log / stores) instead belongs to the Scheduler that drives it, so a
        # child Run keeps the parent's ledger with its own identity.
        self.llm = llm
        self.tools = tools
        # Initial input to fold into shared state on the first drive, committed
        # as the opening state_delta fact so the log is the complete truth (a
        # replay reconstructs the opening user message too). Consumed by that
        # one fact — afterwards it lives in shared.
        self.seed: dict[str, Any] = dict(seed or {})
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
        self.metrics: dict[str, int] = {
            "waves": 0,
            "llm_calls": 0,
            "tool_calls": 0,
            "tokens": 0,
        }

    @property
    def name(self) -> str:
        """The blueprint's name — static identity derived from the plan, never
        run state: it does not change per execution and is not a fact; the
        plan is always at hand wherever the name is needed."""
        return self.plan.name

    # — convenient construction —
    @classmethod
    def start(
        cls,
        plan: Any,
        *,
        task: str = "",
        input: dict[str, Any] | None = None,
        llm: Any = None,
        tools: Any = None,
    ) -> Run:
        plan.validate()
        # ``input`` is the opening state update, supplied by whoever starts the
        # Run (the ReAct layer turns a task into the opening user message); the
        # kernel never derives it from the Plan.
        return cls(plan, task=task, seed=input, llm=llm, tools=tools)

    @classmethod
    def child_of(
        cls,
        parent: Run,
        plan: Any,
        *,
        task: str = "",
        input: dict[str, Any] | None = None,
        llm: Any = None,
        tools: Any = None,
    ) -> Run:
        """Born from a delegation: depth is computed from the parent object, so
        the tree invariant (child = parent + 1) cannot be forgotten or forged at
        a call site — the only way deeper is through the parent."""
        depth = parent.depth + 1
        if depth > _MAX_RUN_DEPTH:
            # Mutual delegation (A activates B, B activates A) would make the
            # Run tree only grow; blocking it structurally is far more reliable
            # than trusting the model to "remember not to call each other".
            raise RecursionError(
                f"Run tree depth exceeds {_MAX_RUN_DEPTH}: "
                "check for a circular delegation between agents"
            )
        plan.validate()
        return cls(
            plan,
            parent_id=parent.run_id,
            depth=depth,
            task=task,
            seed=input,
            llm=llm,
            tools=tools,
        )

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

    @property
    def running(self) -> bool:
        return self.state == RunState.RUNNING

    # — node state queries —
    def state_of(self, key: str) -> NodeRuntimeState:
        return self.node_states[key]

    def is_instance(self, key: str) -> bool:
        return _is_instance_key(key) and key in self.instance_inputs

    def template_of(self, key: str) -> str:
        return key.split("#", 1)[0] if _is_instance_key(key) else key

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

    # — node state changes (the single throat; only Run.apply calls these in production) —
    def mark_running(self, key: str) -> None:
        st = self.node_states[key]
        st.status = NodeStatus.RUNNING
        st.attempts += 1
        # Consume the immediate-activation pass now: without this, a node
        # that was ever Goto(immediate=True)'d would stay in `activated`
        # forever, bypassing predecessor/join gating on every future rearm.
        self.activated.discard(key)

    def mark_completed(self, key: str, output: Any) -> None:
        st = self.node_states[key]
        st.status = NodeStatus.COMPLETED
        st.output = output

    def mark_skipped(self, key: str) -> None:
        self.node_states[key].status = NodeStatus.SKIPPED

    def mark_failed(self, key: str, error: str) -> None:
        st = self.node_states[key]
        st.status = NodeStatus.FAILED
        st.output = error

    def rearm(self, key: str, *, immediate: bool = False) -> None:
        """Re-arm: move a (possibly COMPLETED) node back to PENDING — the one
        low-level action that lets a node run again.

        immediate=True also adds it to ``activated``, so next wave it bypasses
        its predecessors and becomes ready right away (sequential back-edges,
        runtime jumps, handovers). immediate=False lets incoming edges and join
        decide when it runs again (an iterative convergence point waits for
        this wave's predecessors)."""
        st = self.node_states.setdefault(key, NodeRuntimeState())
        st.status = NodeStatus.PENDING
        if immediate:
            self.activated.add(key)

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

    # — wave barrier: aggregate this wave's deltas into one fact —
    def wave_delta(self, writes: list[Any], channels: dict[str, Channel]) -> dict[str, Any]:
        """Aggregate the wave's writes into the "wave delta" — pure, mutates
        nothing. Multiple writes to one channel within the wave fold from the
        channel's identity element first, so the event stores "what this wave
        added" and replaying wave by wave rebuilds state without
        double-counting. Folding it into shared state is apply(STATE_DELTA)'s
        job — the one throat.
        """
        out: dict[str, Any] = {}
        for w in writes:
            channel = channels[w.key]
            base = out.get(w.key, channel.empty)
            out[w.key] = channel.reducer(base, w.value)
        return out

    # — the single mutation throat —
    def apply(self, ev: Event) -> None:
        """Fold one recorded fact into this Run — the ONLY way ledger state
        (everything scheduling needs: node statuses, shared state, instances,
        deliveries, suspensions, resume values, the terminal state) ever
        changes. The live scheduler commits through here and replay replays
        through here, so the two cannot drift apart by discipline.

        Two honest exemptions, stated as law, not apology: identity is born
        from the opening fact at construction (unborn has nothing to mutate);
        ``metrics`` is engine telemetry — never a fact, never rebuilt, never
        faked. Facts for other projections (DELEGATED, ARTIFACT_WRITTEN,
        NODE_RETRY) change no ledger state and pass through as no-ops.
        """
        if ev.run_id != self.run_id:
            raise ValueError(f"fact belongs to run {ev.run_id!r}, not {self.run_id!r}")
        d = ev.data
        if ev.kind == RUN_STARTED:
            if self.event_seq != 0:
                raise ValueError("run_started may only open a fresh stream")
        elif ev.kind == NODE_STARTED:
            self.mark_running(d["node"])
            self.resume_values.pop(d["node"], None)  # the re-run consumes its answer
        elif ev.kind == NODE_COMPLETED:
            self.mark_completed(d["node"], d.get("output"))
            self.deliveries.pop(d["node"], None)  # a Goto input dies at terminal state
        elif ev.kind == NODE_FAILED:
            self.mark_failed(d["node"], d.get("error", ""))
            self.deliveries.pop(d["node"], None)
        elif ev.kind == NODE_SKIPPED:
            self.mark_skipped(d["node"])
        elif ev.kind == STATE_DELTA:
            apply_event(self.shared, ev, self.plan.channels)
        elif ev.kind == CONTROL:
            if d["op"] == "goto":
                self.rearm(d["target"], immediate=d.get("immediate", True))
                if d.get("payload") is not None:
                    self.deliveries[d["target"]] = d["payload"]
            else:  # "send": instantiate a template copy (full key derives in order)
                self.add_instance(d["template"], d.get("payload"), d.get("key"))
        elif ev.kind == INTERRUPTED:
            self.suspend(
                {
                    node: Interrupt(
                        p.get("kind", "external"),
                        p.get("payload"),
                        p.get("question", ""),
                        node,
                    )
                    for node, p in d.get("parked", {}).items()
                }
            )
        elif ev.kind == RESUMED:
            self.resume(d.get("values") or None)
            for node in d.get("nodes", []):
                self.rearm(node, immediate=True)
        elif ev.kind == RUN_COMPLETED:
            self.complete(d.get("output"))
        elif ev.kind == RUN_FAILED:
            self.fail(d.get("reason", ""))
        self.event_seq = ev.seq
