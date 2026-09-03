"""Graph and Plan — topology first, versioned blueprints second.

A :class:`Graph` is pure topology: a set of nodes and a set of
:class:`Edge` objects. Nothing here knows about versions, checkpoints, or
runs — every method that needs progress takes the states as an argument.
That is what lets one hand-written graph serve many runs, and what lets a
:class:`Plan` (a Graph *plus* a version counter and a lineage label) layer
replanning on top without touching the topology layer.

Edges are the runtime truth. ``Node.depends_on`` is declaration sugar —
the form every front-end (model JSON, hand-written Workflow, checkpoint
wire) naturally speaks — and :meth:`Graph.add_nodes` folds it into edges
at construction; the two mutation sites that ever rewire
(:meth:`Plan.merge`) rebuilds both together, so the
declared view never drifts from the edge set. What ONLY lives on an Edge
is the condition: ``when`` is the predicate form of Route — the edge is
active while ``when(shared_state)`` holds, and a waived edge satisfies the
dependency without the source ever running.

The acyclicity law lives one file over (``graph_validator``): every Graph
is acyclic — loops come from the Loop unit's interpreted execution, never
from edges (REFACTOR-PLAN ruling 2).
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from prodagent.base.determinism import new_uuid4
from prodagent.kernel.node_state import NodeRuntimeState
from prodagent.kernel.types import NodeStatus
from prodagent.kernel.units import NodeKind

if TYPE_CHECKING:
    from prodagent.kernel.unit import GraphUnit

if TYPE_CHECKING:
    from collections.abc import Callable


class Origin(StrEnum):
    """Where a node (and the plan carrying it) came from — the trust label
    that survives unification (column 8: unify the format, never flatten
    the lineage).

    STATIC — hand-written, trusted, validated once at compile time.
    CONDITIONAL — hand-written candidates; runtime picks by rule (reserved).
    PLANNED — model-drafted, untrusted, revalidated on every submission.
    DYNAMIC — grown at runtime (reserved; combinators grow at interpretation time).
    """

    STATIC = "static"
    CONDITIONAL = "conditional"
    PLANNED = "planned"
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
    body: GraphUnit
    """Any Unit — the five built-ins (wire-restorable) or a composed one
    (Sequential/Parallel/Route/Loop, a user class). Composed bodies are
    process-local: their wire form records kind and target, but restore
    re-materializes only the built-ins — composition is re-declared in
    code, not persisted (ruling 3: names, never live objects)."""
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: Sequence[str] = ()
    """Declaration sugar for plain incoming edges; conditional edges are
    graph-level (``Graph.edge(..., when=...)``) and never appear here."""
    is_terminal: bool = False
    origin: Origin = Origin.PLANNED
    """Unstated lineage reads as PLANNED — untrusted, revalidated. A wire
    written before origins existed resumes under the conservative label."""

    version_created: int = 1
    replaces_node_id: str | None = None
    """Replan lineage: the node this one replaced. Completed nodes may carry
    side effects (a sent email, a written row) — replans must reroute around
    them, never re-execute them."""

    def __post_init__(self) -> None:
        # Freeze the containers: the frozen dataclass guards the fields, the
        # proxy/tuple guard what's inside them.
        object.__setattr__(self, "params", MappingProxyType(self.params))
        object.__setattr__(self, "depends_on", tuple(self.depends_on))

    @property
    def action(self) -> str:
        """The unit's target name — the one string events, hooks, and the
        wire all use for "what this node invokes"."""
        return self.body.target

    @property
    def kind(self) -> Any:
        """``NodeKind`` for built-in bodies, a plain string for composed ones."""
        return self.body.kind

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
    from prodagent.kernel.units import unit_to_wire_extras

    kind = node.kind
    return {
        "node_id": node.node_id,
        "kind": kind.value if isinstance(kind, NodeKind) else str(kind),
        "action": node.action,
        "origin": node.origin.value,
        **unit_to_wire_extras(node.body),
        "params": dict(node.params),
        "depends_on": list(node.depends_on),
        "is_terminal": node.is_terminal,
        "status": state.status.value,
        "output_ref": state.output_ref,
        "error": state.error,
        "attempts": state.attempts,
        "completed_at": state.completed_at,
        "version_created": node.version_created,
        "replaces_node_id": node.replaces_node_id,
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

    def __init__(self, *, origin: Origin = Origin.PLANNED) -> None:
        self.origin = origin
        self._nodes: dict[str, Node] = {}
        self._incoming: dict[str, list[Edge]] = {}
        # Reverse index (source -> dependents) so "what becomes ready when
        # this node lands" is a lookup, not a graph rescan.
        self._dependents: dict[str, list[str]] = {}

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
    ) -> Edge:
        """Add one edge explicitly — the conditional form's entry point."""
        e = Edge(source=source, target=target, when=when)
        self._add_edge(e)
        return e

    def _add_edge(self, e: Edge) -> None:
        self._incoming.setdefault(e.target, []).append(e)
        self._dependents.setdefault(e.source, []).append(e.target)

    def _reindex(self) -> None:
        edges = [e for es in self._incoming.values() for e in es]
        self._incoming = {}
        self._dependents = {}
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
        (the source does not feed this target). A node every incoming edge
        of which is waived is not ready at all — it is *skipped* (see
        :meth:`skipped`). Read-only tools racing inside a node is a
        lower-level concern; *this* is the DAG's parallelism unit."""
        return [
            n
            for n in self._nodes.values()
            if state_of(states, n.node_id).status is NodeStatus.PENDING
            and not self._all_edges_waived(n.node_id, shared)
            and self._deps_done(n, states, shared)
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
        not taken. They will never run; the driver marks them OBSOLETE so
        the graph converges. (A node with no incoming edges is a root and
        never skipped. The verdict is permanent for the run: a branch not
        taken stays not taken.)"""
        return [
            n
            for n in self._nodes.values()
            if state_of(states, n.node_id).status is NodeStatus.PENDING
            and self._all_edges_waived(n.node_id, shared)
        ]

    def is_complete(self, states: Mapping[str, NodeRuntimeState]) -> bool:
        """OBSOLETE counts as done: a node scrapped by replan neither ran nor
        failed — it simply stopped mattering."""
        return all(
            state_of(states, n.node_id).status in (NodeStatus.COMPLETED, NodeStatus.OBSOLETE)
            for n in self._nodes.values()
        )

    def _deps_done(
        self,
        node: Node,
        states: Mapping[str, NodeRuntimeState],
        shared: Mapping[str, Any] | None = None,
    ) -> bool:
        for e in self._incoming.get(node.node_id, ()):
            if not e.is_active(shared):
                continue  # waived — this source does not feed the target
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

    def __init__(self, plan_id: str | None = None, *, origin: Origin = Origin.PLANNED) -> None:
        super().__init__(origin=origin)
        self.plan_id = plan_id or new_uuid4()
        self.version: int = 1
        self.task_input: str = ""

    # ── Replan — new version, never in-place mutation ──────────────────────

    def derive(self, *, plan_id: str, task_input: str) -> Plan:
        """Same blueprint, new identity — how a reusable template becomes
        this run's plan without copying (nodes and edges are frozen and
        shared)."""
        derived = Plan(plan_id=plan_id, origin=self.origin)
        derived.version = self.version
        derived.task_input = task_input
        derived._nodes = dict(self._nodes)
        derived._incoming = {k: list(v) for k, v in self._incoming.items()}
        derived._reindex()
        return derived

    def merge(self, new_nodes: list[Node], states: Mapping[str, NodeRuntimeState]) -> Plan:
        """Fold replacement nodes into the next plan version.

        Returns a new Plan (version + 1) sharing every untouched node; the
        *states* side is updated in place — replaced nodes flip to OBSOLETE
        where the caller can see it, because "replaced" is progress, not
        blueprint. Dependency validation runs against the merged graph so a
        replan can re-link onto surviving nodes, and acyclicity is asserted
        before anything lands."""
        new_ids = {n.node_id for n in new_nodes}
        for nn in new_nodes:
            for dep in nn.depends_on:
                if (
                    dep in self._nodes
                    and state_of(states, dep).status is NodeStatus.OBSOLETE
                    and dep not in new_ids
                ):
                    raise ValueError(
                        f"Node {nn.node_id!r}: dependency {dep!r} is OBSOLETE "
                        f"(and not replaced in this merge)"
                    )

        # One validator, five checks, over the live union — the same gate
        # every other birth line passes through (dangling refs name the
        # offender; cycles name their members).
        live = [
            n
            for n in self._nodes.values()
            if state_of(states, n.node_id).status is not NodeStatus.OBSOLETE
        ]
        from prodagent.kernel.graph_validator import default_validator

        default_validator().validate_nodes([*live, *new_nodes])

        merged = Plan(plan_id=self.plan_id, origin=self.origin)
        merged.version = self.version + 1  # every replan advances the version — lineage
        merged.task_input = self.task_input
        merged._nodes = dict(self._nodes)
        merged._incoming = {k: list(v) for k, v in self._incoming.items()}
        for nn in new_nodes:
            stamped = replace(nn, version_created=merged.version)
            if stamped.replaces_node_id:
                old_state = states.get(stamped.replaces_node_id)
                if old_state is not None:
                    old_state.mark_obsolete()  # replaced, not deleted — history replays
            merged._nodes[stamped.node_id] = stamped
            for dep in stamped.depends_on:
                merged._add_edge(Edge(source=dep, target=stamped.node_id))
        merged._reindex()
        return merged

    def mark_downstream_obsolete(
        self, failed_node_id: str, states: dict[str, NodeRuntimeState]
    ) -> list[str]:
        """On failure, quarantine everything that (transitively) depended on
        the failed node — their inputs will never materialize.

        COMPLETED dependents are skipped but traversed so their own PENDING
        downstream still gets obsoleted: a finished node's failure-cousins
        down the chain are just as doomed as direct dependents."""
        obsolete: list[str] = []
        queue: deque[str] = deque(self._dependents.get(failed_node_id, ()))
        seen: set[str] = set(queue)

        while queue:
            nid = queue.popleft()
            st = states.get(nid)
            if st is None:
                continue
            if st.status is not NodeStatus.COMPLETED:
                st.mark_obsolete()
                obsolete.append(nid)
            for child_id in self._dependents.get(nid, ()):
                if child_id not in seen:
                    seen.add(child_id)
                    queue.append(child_id)

        return obsolete

    # ── Durable wire (the checkpoint/event form predates the split) ────────

    def to_state(self, states: Mapping[str, NodeRuntimeState]) -> dict[str, Any]:
        """The dict that rides inside the checkpoint's plan cursor — version
        plus every node's static fields *and* progress, so ``from_state``
        rebuilds both halves losslessly."""
        return {
            "version": self.version,
            "nodes": {
                n.node_id: node_wire_dict(n, state_of(states, n.node_id))
                for n in self._nodes.values()
            },
        }

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
        node_states: dict[str, NodeRuntimeState] = {}
        for nid, nd in state.get("nodes", {}).items():
            from prodagent.kernel.units import unit_from_wire

            plan._nodes[nid] = Node(
                node_id=nid,
                body=unit_from_wire(nd.get("kind", ""), nd.get("action", ""), nd),
                params=nd.get("params", {}),
                depends_on=nd.get("depends_on", []),
                is_terminal=nd.get("is_terminal", False),
                origin=Origin(nd.get("origin", Origin.PLANNED.value)),
                version_created=nd.get("version_created", plan.version),
                replaces_node_id=nd.get("replaces_node_id"),
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


@dataclass(frozen=True)
class PlanDraft:
    """Parsed nodes (empty means no plan) + raw response text from one
    planning call — parse success and raw evidence travel together so a
    bad draft is auditable against what the model actually said. The
    kernel's :class:`~prodagent.kernel.scheduler.PlannerPort` speaks this
    type; the LLM implementation lives above the kernel."""

    nodes: list[Node]
    raw_text: str


def compile_planned(nodes: list[Node], *, revision: int = 1) -> Plan:
    """Planner output → validate(origin=PLANNED) → Plan.

    Every model draft revalidates: the model is an untrusted front-end
    that hallucinated edges and cycles will keep doing so."""
    from prodagent.kernel.graph_validator import PlanValidator

    PlanValidator().validate_nodes(nodes)
    plan = Plan(origin=Origin.PLANNED)
    plan.add_nodes(list(nodes))
    return plan


__all__ = [
    "Origin",
    "Edge",
    "PlanDraft",
    "Node",
    "Graph",
    "Plan",
    "fresh_states",
    "state_of",
    "node_wire_dict",
    "compile_planned",
]
