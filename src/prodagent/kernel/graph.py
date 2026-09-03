"""Graph and Plan — topology first, versioned blueprints second.

A :class:`Graph` is pure topology: a set of nodes and a set of
:class:`Edge` objects. Nothing here knows about versions, checkpoints, or
runs — every method that needs progress takes the states as an argument.
That is what lets one hand-written graph serve many runs, and what lets a
:class:`Plan` (a Graph *plus* a version counter and a lineage label) layer
replanning on top without touching the topology layer.

Edges are the runtime truth. ``Node.depends_on`` is declaration sugar —
the form every front-end (hand-written Workflow, checkpoint wire) naturally
speaks — and :meth:`Graph.add_nodes` folds it into edges at construction;
the one mutation site that ever grows the set (:meth:`Send` instantiation)
goes through ``add_nodes`` too, so the declared view never drifts from the
edge set. What ONLY lives on an Edge
is the condition: ``when`` is the predicate form of Route — the edge is
active while ``when(shared_state)`` holds, and a waived edge satisfies the
dependency without the source ever running.

Cycles are legal (column 5/6: agents iterate, so the execution graph is a
graph-that-may-loop and a DAG is just the loop-free special case). An edge
that closes a cycle is a *back edge* — :meth:`Graph.back_edges` — and back
edges never gate readiness (first activation comes from forward edges
only, or a cycle could never start). Re-activation is the engine's
requeue: when a node succeeds, every active outgoing edge whose target is
already COMPLETED puts that target back to PENDING — a back edge restarting
its loop, a forward edge cascading a redo (a re-run source's old output is
stale for its dependents). Termination is the blueprint's promise (a back
edge carries or implies its exit condition); the engine's guards — the
wave cap, the empty-ready :class:`Stalled`, and the no-progress detector —
are the backstop, not the proof.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from prodagent.base.determinism import new_uuid4
from prodagent.kernel.bodies import NodeKind
from prodagent.kernel.node_state import NodeRuntimeState
from prodagent.kernel.types import NodeStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.kernel.body import NodeBody
    from prodagent.kernel.channels import Channel


class Origin(StrEnum):
    """Where a node came from — the trust label that survives the wire.

    STATIC — declared in code, trusted, validated once at compile time.
    DYNAMIC — grown at runtime (a Send's instantiated template, a
    combinator's compiled shape).

    The old PLANNED label (a model-drafted graph) is gone with the planner:
    models produce task lists, not graphs (column 24). Legacy wire values
    read back as STATIC (coerced at the read site)."""

    STATIC = "static"
    DYNAMIC = "dynamic"


@dataclass(frozen=True)
class Edge:
    """One directed edge: ``source`` must reach a terminal state before
    ``target`` is ready — unless the condition waives it.

    ``when`` is Route's underlying form (and the only conditional control
    flow the graph has): ``None`` is a hard edge; a callable is evaluated
    against the run's shared state when readiness is computed — active
    while True, waived while False. A waived edge satisfies the dependency
    without the source running: that is how a Route's non-taken branches
    step aside."""

    source: str
    target: str
    when: Callable[[Mapping[str, Any]], bool] | None = None
    back: bool | None = None
    """Back-edge role (column 5): ``True`` forces it a back edge (the loop's
    requeue trigger, never a readiness gate); ``False`` forces it a forward
    edge (a loop's *entry* edge — it gates readiness even though it sits in
    a cycle); ``None`` lets the topology decide (auto-detect the cycle)."""

    def is_active(self, shared: Mapping[str, Any] | None = None) -> bool:
        if self.when is None:
            return True
        return bool(self.when(shared or {}))


@dataclass(frozen=True)
class Node:
    """One step of the blueprint: what to run (the unit) and what it waits
    for (declared incoming edges — folded into the graph's edge set at
    ``add_nodes`` time).

    Static only — status/output/attempts live on the run's
    :class:`NodeRuntimeState`. Frozen: a Node is shared by every run of the
    graph and referenced across replan versions, so nothing about it may
    change after construction."""

    node_id: str
    body: NodeBody
    """Any NodeBody — the five built-ins (wire-restorable) or a composed one
    (Sequential/Parallel/Route/Loop, a user class). Composed bodies are
    process-local: their wire form records kind and target, but restore
    re-materializes only the built-ins — composition is re-declared in
    code, not persisted (ruling 3: names, never live objects)."""
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: Sequence[str] = ()
    """Declaration sugar for plain incoming edges; conditional edges are
    graph-level (``Graph.edge(..., when=...)``) and never appear here."""
    is_terminal: bool = False
    is_template: bool = False
    """A Send target (column 17): the node is a *template*, never executed
    itself — Sends instantiate copies of it at runtime. Excluded from
    readiness and from completion's demands; its body and params are the
    stamp the instances carry."""
    join: Literal["all", "any"] = "all"
    """How multiple incoming edges gate readiness (column 5): ``all``
    waits for every active source (the barrier), ``any`` proceeds on the
    first one done. A conditional branch's merge is ``any`` — exactly one
    branch runs, the other is SKIPPED — while a fan-in is ``all``."""
    origin: Origin = Origin.STATIC
    """Unstated lineage reads as STATIC — code-declared is the default the
    compile-time validator has already gated."""

    def __post_init__(self) -> None:
        # Freeze the containers: the frozen dataclass guards the fields, the
        # proxy/tuple guard what's inside them.
        object.__setattr__(self, "params", MappingProxyType(self.params))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))

    @property
    def action(self) -> str:
        """The body's target name — the one string events, hooks, and the
        wire all use for "what this node invokes"."""
        return str(getattr(self.body, "target", ""))

    @property
    def kind(self) -> Any:
        """``NodeKind`` for built-in bodies, a plain string for composed ones."""
        return getattr(self.body, "kind", None)

    def to_hook_dict(self, *, include_terminal: bool = False) -> dict[str, Any]:
        """Hook/event form: only what an observer needs. Slimmer than the
        checkpoint form on purpose — hooks fire per node, payload bytes add up."""
        d: dict[str, Any] = {
            "id": self.node_id,
            "action": self.action,
            "kind": self.kind.value if isinstance(self.kind, NodeKind) else str(self.kind),
            "params": dict(self.params),
            "depends_on": list(self.depends_on),
        }
        if include_terminal:
            d["terminal"] = self.is_terminal
        return d


def node_wire_dict(node: Node, state: NodeRuntimeState) -> dict[str, Any]:
    """Checkpoint/event form of a node — static fields from the blueprint
    (kind, target, unit extras), progress fields from the run's state. One
    dict, both halves: the durable wire predates the Graph/Plan split and
    stays stable across it."""
    from prodagent.kernel.bodies import body_to_wire_extras

    kind = node.kind
    return {
        "node_id": node.node_id,
        "kind": kind.value if isinstance(kind, NodeKind) else str(kind),
        "action": node.action,
        "origin": node.origin.value,
        **body_to_wire_extras(node.body),
        "params": dict(node.params),
        "depends_on": list(node.depends_on),
        "is_terminal": node.is_terminal,
        "is_template": node.is_template,
        "join": node.join,
        "status": state.status.value,
        "output_ref": state.output_ref,
        "error": state.error,
        "attempts": state.attempts,
        "completed_at": state.completed_at,
    }


def fresh_states(graph: Graph) -> dict[str, NodeRuntimeState]:
    """All-PENDING states for a graph — the starting point of a new run."""
    return {n.node_id: NodeRuntimeState(n.node_id) for n in graph.nodes.values()}


def state_of(states: Mapping[str, NodeRuntimeState], node_id: str) -> NodeRuntimeState:
    """Read a node's state, vacuously PENDING when untouched so far."""
    st = states.get(node_id)
    return st if st is not None else NodeRuntimeState(node_id)


class Graph:
    """Pure topology: nodes + edges. Reusable across runs by construction —
    nothing here is per-run state."""

    def __init__(self, *, origin: Origin = Origin.STATIC) -> None:
        self.origin = origin
        self._nodes: dict[str, Node] = {}
        self._incoming: dict[str, list[Edge]] = {}
        # Reverse index (source -> dependents) so "what becomes ready when
        # this node lands" is a lookup, not a graph rescan.
        self._dependents: dict[str, list[str]] = {}
        self._outgoing: dict[str, list[Edge]] = {}
        self._back_edges: frozenset[Edge] | None = None

    # ── Construction ────────────────────────────────────────────────────────

    def add_nodes(
        self,
        nodes: list[Node],
        *,
        deps: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """Initial insertion (planner output / Workflow compile). Each
        node's declared ``depends_on`` — plus the optional ``deps`` map —
        becomes edges: the edge set is the runtime truth from here on.
        Shape checks live in the validator; every birth line (compiler,
        planner, merge) validates before calling this."""
        for n in nodes:
            self._nodes[n.node_id] = n
            for dep in (*n.depends_on, *(deps.get(n.node_id, ()) if deps else ())):
                self._add_edge(Edge(source=dep, target=n.node_id))

    def edge(
        self,
        source: str,
        target: str,
        *,
        when: Callable[[Mapping[str, Any]], bool] | None = None,
        back: bool | None = None,
    ) -> Edge:
        """Add one edge explicitly — the conditional form's entry point.
        ``back`` forces the edge's back-edge role (see :class:`Edge.back`)."""
        e = Edge(source=source, target=target, when=when, back=back)
        self._add_edge(e)
        return e

    def _add_edge(self, e: Edge) -> None:
        self._incoming.setdefault(e.target, []).append(e)
        self._dependents.setdefault(e.source, []).append(e.target)
        self._outgoing.setdefault(e.source, []).append(e)
        self._back_edges = None

    def _reindex(self) -> None:
        edges = [e for es in self._incoming.values() for e in es]
        self._incoming = {}
        self._dependents = {}
        self._outgoing = {}
        for e in edges:
            self._add_edge(e)

    # ── Reads (progress comes in as an argument) ───────────────────────────

    @property
    def nodes(self) -> dict[str, Node]:
        return dict(self._nodes)

    @property
    def edges(self) -> list[Edge]:
        return [e for es in self._incoming.values() for e in es]

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def incoming(self, node_id: str) -> list[Edge]:
        """The edges pointing at ``node_id`` (declared and conditional)."""
        return list(self._incoming.get(node_id, ()))

    def outgoing(self, node_id: str) -> list[Edge]:
        """The edges leaving ``node_id`` — what its completion can wake."""
        return list(self._outgoing.get(node_id, ()))

    def back_edges(self) -> frozenset[Edge]:
        """The edges that close a cycle (column 5's 回边): source→target
        where the target already reaches the source. These are the engine's
        only implicit requeue trigger — a forward edge into a completed node
        never restarts it. An explicit ``back=`` tag overrides the topology:
        ``back=True`` is a back edge even if drawn against the flow,
        ``back=False`` is a *forward* edge even inside a cycle (a loop's
        entry edge — it gates readiness, it doesn't requeue). Cached per
        topology version."""
        if self._back_edges is None:
            back: set[Edge] = set()
            for edges in self._outgoing.values():
                for e in edges:
                    if (
                        e.back is True
                        or e.back is None
                        and (e.target == e.source or self._reaches(e.target, e.source, skip=e))
                    ):
                        back.add(e)
            self._back_edges = frozenset(back)
        return self._back_edges

    def _reaches(self, start: str, goal: str, *, skip: Edge | None = None) -> bool:
        """Does a path start ⇝ goal exist (optionally ignoring one edge)?"""
        seen = {start}
        queue = deque([start])
        while queue:
            nid = queue.popleft()
            for e in self._outgoing.get(nid, ()):
                if e is skip:
                    continue
                if e.target == goal:
                    return True
                if e.target not in seen:
                    seen.add(e.target)
                    queue.append(e.target)
        return False

    def deps_of(self, node_id: str) -> tuple[str, ...]:
        """The wire/hook view of a node's dependencies — derived from edges."""
        return tuple(e.source for e in self._incoming.get(node_id, ()))

    def dependents_of(self, node_id: str) -> list[str]:
        return list(self._dependents.get(node_id, ()))

    def ready(
        self,
        states: Mapping[str, NodeRuntimeState],
        shared: Mapping[str, Any] | None = None,
    ) -> list[Node]:
        """PENDING nodes whose dependencies are all satisfied — the wave the
        executor fans out concurrently. A dependency is satisfied when its
        source is COMPLETED; a waived conditional edge contributes nothing
        (the source does not feed this target). Back edges never gate
        readiness — first activation comes from the forward edges only, or
        a cycle could never start (the node would wait on a source that is
        itself waiting on it); re-activation flows through the requeue the
        back edge triggers instead. A node every incoming edge of which is
        waived is not ready at all — it is *skipped* (see :meth:`skipped`).
        Read-only tools racing inside a node is a lower-level concern;
        *this* is the graph's parallelism unit."""
        back = self.back_edges()
        return [
            n
            for n in self._nodes.values()
            if not n.is_template
            and state_of(states, n.node_id).status is NodeStatus.PENDING
            and not self._all_edges_waived(n.node_id, shared)
            and self._deps_done(n, states, shared, back_edges=back)
        ]

    def _all_edges_waived(self, node_id: str, shared: Mapping[str, Any] | None) -> bool:
        edges = self._incoming.get(node_id, ())
        return bool(edges) and all(not e.is_active(shared) for e in edges)

    def skipped(
        self,
        states: Mapping[str, NodeRuntimeState],
        shared: Mapping[str, Any] | None = None,
    ) -> list[Node]:
        """PENDING nodes whose incoming edges are ALL waived — Route's roads
        not taken. They will never run; the driver marks them SKIPPED so
        the graph converges. (A node with no incoming edges is a root and
        never skipped. The verdict is permanent for the run: a branch not
        taken stays not taken.)

        Two guards against scrapping a node that is only *temporarily*
        starved:
        - a waived source that hasn't COMPLETED yet may still write the
          state its edge's predicate reads — wait for it, don't scrap;
        - a waived source in an *active back edge's* target set is about to
          re-run and re-decide — wait for it, don't scrap.
        Only a node whose sources are all done *and* done-for is SKIPPED."""
        active_back_targets = {e.target for e in self.back_edges() if e.is_active(shared)}
        out: list[Node] = []
        for n in self._nodes.values():
            if state_of(states, n.node_id).status is not NodeStatus.PENDING:
                continue
            if not self._all_edges_waived(n.node_id, shared):
                continue
            sources = {e.source for e in self._incoming.get(n.node_id, ())}
            if any(state_of(states, s).status is not NodeStatus.COMPLETED for s in sources):
                continue  # a source may still write the predicate's input
            if sources & active_back_targets:
                continue  # a source is about to re-run — wait, don't scrap
            out.append(n)
        return out

    def is_complete(self, states: Mapping[str, NodeRuntimeState]) -> bool:
        """SKIPPED counts as done: a node scrapped by a waived branch or a
        failure's quarantine neither ran nor failed — it simply stopped
        mattering. Templates never run, so they never stand between a run
        and its completion."""
        return all(
            n.is_template
            or state_of(states, n.node_id).status in (NodeStatus.COMPLETED, NodeStatus.SKIPPED)
            for n in self._nodes.values()
        )

    def _deps_done(
        self,
        node: Node,
        states: Mapping[str, NodeRuntimeState],
        shared: Mapping[str, Any] | None = None,
        *,
        back_edges: frozenset[Edge] | None = None,
    ) -> bool:
        def active(e: Edge) -> bool:
            if not e.is_active(shared):
                return False  # waived — this source does not feed the target
            # a back edge is a requeue trigger, never a wait
            return back_edges is None or e not in back_edges

        incoming = [e for e in self._incoming.get(node.node_id, ()) if active(e)]
        if node.join == "any":
            # First-completed wins (column 5): a conditional merge where
            # exactly one branch runs — the SKIPPED one never counts.
            return any(state_of(states, e.source).status is NodeStatus.COMPLETED for e in incoming)
        for e in incoming:
            dep = self._nodes.get(e.source)
            if dep is None:
                raise ValueError(
                    f"Node {node.node_id!r} references unknown dependency {e.source!r}. "
                    "Graph blueprint is corrupted."
                )
            if state_of(states, e.source).status is not NodeStatus.COMPLETED:
                return False
        return True

    def validate(self, fn_sigs: Mapping[str, Any] | None = None) -> None:
        """The five-check gate over this graph — see ``graph_validator``."""
        from prodagent.kernel.graph_validator import default_validator

        default_validator().validate_graph(self, fn_sigs=fn_sigs)


class Plan(Graph):
    """A versioned blueprint a run executes — Graph topology plus lineage.

    Static by construction: nodes are frozen, the graph is built once, and a
    replan doesn't rewrite it in place — it produces the next *version*,
    with replaced nodes carrying lineage (``replaces_node_id``) so the event
    log can replay every revision and resume never mistakes "never ran" for
    "ran and was scrapped".

    How far execution has gotten does **not** live here — that is
    :class:`prodagent.kernel.node_state.NodeRuntimeState`, held by the run.
    Every method that needs progress takes the states as an argument: the
    blueprint answers "what would be ready if execution were here", the run
    answers where execution actually is."""

    def __init__(self, plan_id: str | None = None, *, origin: Origin = Origin.STATIC) -> None:
        super().__init__(origin=origin)
        self.plan_id = plan_id or new_uuid4()
        self.version: int = 1
        self.task_input: str = ""
        self.channels: dict[str, Channel] = {}
        """State's merge rules, declared on the blueprint (column 7): the
        *rules* live here, the folded values live in ``run.shared``."""

    def declare_channels(self, channels: Mapping[str, Any]) -> None:
        """Adopt named state lanes — the Plan's third component. Accepts
        :class:`~prodagent.kernel.channels.Channel` values or wire dicts;
        a plan with no declared channels keeps the legacy immediate-apply
        write path."""
        from prodagent.kernel.channels import Channel, channel_from_wire

        for name, channel in channels.items():
            self.channels[name] = (
                channel if isinstance(channel, Channel) else channel_from_wire(channel)
            )

    # ── Replan — new version, never in-place mutation ──────────────────────

    def derive(self, *, plan_id: str, task_input: str) -> Plan:
        """Same blueprint, new identity — how a reusable template becomes
        this run's plan without copying (nodes and edges are frozen and
        shared)."""
        derived = Plan(plan_id=plan_id, origin=self.origin)
        derived.version = self.version
        derived.task_input = task_input
        derived.channels = dict(self.channels)
        derived._nodes = dict(self._nodes)
        derived._incoming = {k: list(v) for k, v in self._incoming.items()}
        derived._reindex()
        return derived

    def mark_downstream_skipped(
        self, failed_node_id: str, states: dict[str, NodeRuntimeState]
    ) -> list[str]:
        """On failure, quarantine everything that (transitively) depended on
        the failed node — their inputs will never materialize.

        COMPLETED dependents are skipped but traversed so their own PENDING
        downstream still gets skipped: a finished node's failure-cousins
        down the chain are just as doomed as direct dependents."""
        skipped: list[str] = []
        queue: deque[str] = deque(self._dependents.get(failed_node_id, ()))
        seen: set[str] = set(queue)

        while queue:
            nid = queue.popleft()
            st = states.get(nid)
            if st is None:
                continue
            if st.status is not NodeStatus.COMPLETED:
                st.mark_skipped()
                skipped.append(nid)
            for child_id in self._dependents.get(nid, ()):
                if child_id not in seen:
                    seen.add(child_id)
                    queue.append(child_id)

        return skipped

    # ── Durable wire (the checkpoint/event form predates the split) ────────

    def to_state(self, states: Mapping[str, NodeRuntimeState]) -> dict[str, Any]:
        """The dict that rides inside the checkpoint's plan cursor — version
        plus every node's static fields *and* progress, so ``from_state``
        rebuilds both halves losslessly."""
        state = {
            "version": self.version,
            "nodes": {
                n.node_id: node_wire_dict(n, state_of(states, n.node_id))
                for n in self._nodes.values()
            },
        }
        if self.channels:
            state["channels"] = {name: ch.to_wire() for name, ch in self.channels.items()}
        return state

    @classmethod
    def from_state(
        cls, state: dict[str, Any], *, plan_id: str
    ) -> tuple[Plan, dict[str, NodeRuntimeState]]:
        """Checkpoint-restore path: the resume half of crash recovery.
        Returns the blueprint and the per-node states it resumes with.

        The RUNNING→PENDING reset below is the DAG-level resume rule: a node
        found mid-flight at crash time has unknown partial state, so it is
        redone; COMPLETED nodes are never re-executed (their side effects
        already happened)."""
        plan = cls(plan_id=plan_id)
        plan.version = state.get("version", 1)
        if state.get("channels"):
            plan.declare_channels(state["channels"])
        node_states: dict[str, NodeRuntimeState] = {}
        for nid, nd in state.get("nodes", {}).items():
            from prodagent.kernel.bodies import body_from_wire

            plan._nodes[nid] = Node(
                node_id=nid,
                body=body_from_wire(nd.get("kind", ""), nd.get("action", ""), nd),
                params=nd.get("params", {}),
                depends_on=nd.get("depends_on", []),
                is_terminal=nd.get("is_terminal", False),
                is_template=nd.get("is_template", False),
                join=nd.get("join", "all"),
                origin=_origin_of(nd.get("origin")),
            )
            for dep in nd.get("depends_on", []):
                plan._add_edge(Edge(source=dep, target=nid))
            st = NodeRuntimeState(
                node_id=nid,
                status=NodeStatus(nd.get("status", "pending")),
                output_ref=nd.get("output_ref"),
                error=nd.get("error"),
                attempts=nd.get("attempts", 0),
                completed_at=nd.get("completed_at", 0.0),
            )
            if st.status is NodeStatus.RUNNING:
                # Crashed mid-flight: output/error are stale partial state.
                st.status = NodeStatus.PENDING
                st.output_ref = None
                st.error = None
            node_states[nid] = st
        return plan, node_states

    # ── Data flow along the edges ──────────────────────────────────────────

    def resolve_params(
        self,
        node: Node,
        states: Mapping[str, NodeRuntimeState],
        shared: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Expand ``{{node_id.output}}`` template refs in a node's params into
        upstream outputs (and ``{{shared.key}}`` into the run's shared
        state), right before the node runs — data flows along the graph's
        edges without the executor knowing any wiring."""
        result = _resolve(node.params, node, self, states, shared or {})
        return result if isinstance(result, dict) else {}


_TEMPLATE_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_LOOSE_TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")


def _resolve(
    value: Any,
    node: Node,
    graph: Graph,
    states: Mapping[str, NodeRuntimeState],
    shared: Mapping[str, Any],
) -> Any:
    """Recursive template expansion over params. Whole-string refs preserve
    the referenced value's type (a dict stays a dict); embedded refs must
    stringify. Unknown-but-template-looking syntax is a hard error with the
    offender named — silent passthrough would surface much later as a tool
    receiving the literal string ``"{{x.output}}"``."""
    if isinstance(value, Mapping):
        return {k: _resolve(v, node, graph, states, shared) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, node, graph, states, shared) for v in value]
    if not isinstance(value, str):
        return value

    matches = list(_TEMPLATE_RE.finditer(value))
    if not matches:
        if _LOOSE_TEMPLATE_RE.search(value):
            invalid = _LOOSE_TEMPLATE_RE.findall(value)
            raise ValueError(
                f"param chain: node {node.node_id!r} has unsupported template syntax "
                f"{invalid[0]!r}. Only '{{{{node_id}}}}', '{{{{node_id.output}}}}', or "
                f"'{{{{node_id.output.field}}}}' (single top-level key, no array index, "
                "no nested path) are supported. Hardcode the concrete value instead."
            )
        return value
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return _lookup(matches[0].group(1), node, graph, states, shared)
    return _TEMPLATE_RE.sub(lambda m: str(_lookup(m.group(1), node, graph, states, shared)), value)


def _lookup(
    ref: str,
    node: Node,
    graph: Graph,
    states: Mapping[str, NodeRuntimeState],
    shared: Mapping[str, Any],
) -> Any:
    """Resolve one ``{{...}}`` reference against the graph's completed nodes.

    Two extra rules beyond plain lookup: ``{{task}}`` reaches the original
    user task, and referring to a node that is not COMPLETED is an error —
    the scheduler only runs dependency-satisfied nodes, so hitting this
    means the graph and the ref disagree (a bug, not a race)."""
    parts = ref.split(".")
    dep_id = parts[0]
    rest = parts[1:]

    if dep_id == "shared":
        # The run's shared state: what Update commands merged. Only
        # whole-key and single-field refs, same as node outputs.
        if not rest:
            raise ValueError(
                f"param chain: node {node.node_id!r} — {{shared}} needs a key, "
                "got a bare {{shared}}"
            )
        if len(rest) != 1:
            raise ValueError(
                f"param chain: node {node.node_id!r} — only {{shared.key}} is "
                f"supported, got {ref!r}"
            )
        if rest[0] not in shared:
            raise ValueError(f"param chain: shared state has no key {rest[0]!r}")
        return shared[rest[0]]

    if dep_id == "task":
        if rest:
            raise ValueError(
                f"param chain: node {node.node_id!r} — {{task}} has no sub-fields, got {ref!r}"
            )
        plan = graph if isinstance(graph, Plan) else None
        if plan is None:
            raise ValueError("param chain: {{task}} needs a Plan (task_input lives there)")
        return plan.task_input

    if rest and rest[0] == "output":
        rest = rest[1:]
    dep = graph.get_node(dep_id)
    if dep is None:
        raise ValueError(
            f"param chain: node {node.node_id!r} references {dep_id!r} which is not in the graph"
        )
    st = state_of(states, dep_id)
    if st.status is not NodeStatus.COMPLETED:
        raise ValueError(
            f"param chain: node {node.node_id!r} references {dep_id!r} but it is "
            f"{st.status.value!r}, not COMPLETED"
        )
    if not rest:
        return st.output_ref
    if len(rest) != 1:
        raise ValueError(
            f"param chain: node {node.node_id!r} — only single-key refs "
            f"({{node_id.field}} or {{node_id.output.field}} or {{node_id.output}}) "
            f"are supported, got {ref!r}"
        )
    key = rest[0]
    if not isinstance(st.output_ref, dict) or key not in st.output_ref:
        raise ValueError(f"param chain: node {dep_id!r}.output has no key {key!r}")
    return st.output_ref[key]


def _origin_of(value: object) -> Origin:
    """Wire origin → Origin, with legacy labels reading as STATIC."""
    try:
        return Origin(str(value))
    except ValueError:
        return Origin.STATIC


def compile_planned(nodes: list[Node]) -> Plan:
    """A node list → validate → Plan: the generic birth line every code
    front-end shares (Workflow compile, tests, embedders). Validation runs
    here so no plan enters execution ungated — whoever wrote the nodes,
    the shape checks are the same."""
    from prodagent.kernel.graph_validator import PlanValidator

    PlanValidator().validate_nodes(nodes)
    plan = Plan(origin=Origin.STATIC)
    plan.add_nodes(list(nodes))
    return plan


__all__ = [
    "Origin",
    "Edge",
    "Node",
    "Graph",
    "Plan",
    "fresh_states",
    "state_of",
    "node_wire_dict",
    "compile_planned",
]
