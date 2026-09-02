"""Plan — the immutable blueprint of a run: nodes, edges, lineage.

A Plan is what the planner (model or hand-written Workflow) drafts and what
a run executes. It is static by construction: nodes are frozen, the graph
is built once, and a replan doesn't rewrite it in place — it produces the
next *version*, with replaced nodes carrying lineage
(``replaces_node_id``) so the event log can replay every revision and
resume never mistakes "never ran" for "ran and was scrapped".

How far execution has gotten does **not** live here — that is
:class:`prodagent.kernel.node_state.NodeRuntimeState`, held by the run.
Every Plan method that needs progress takes the states as an argument:
the blueprint answers "what would be ready if execution were here", the
run answers where execution actually is.
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
from prodagent.kernel.bodies.base import (
    NodeBody,
    NodeKind,
    ReActBody,
    body_from_wire,
    body_to_wire_extras,
)
from prodagent.kernel.node_state import NodeRuntimeState
from prodagent.kernel.types import NodeStatus

if TYPE_CHECKING:
    from prodagent.plan.ir.validator import PlanValidator


class Origin(StrEnum):
    """Where a node (and the plan carrying it) came from — the trust label
    that survives unification (column 8: unify the format, never flatten
    the lineage).

    STATIC — hand-written, trusted, validated once at compile time.
    CONDITIONAL — hand-written candidates; runtime picks by rule (reserved).
    PLANNED — model-drafted, untrusted, revalidated on every submission.
    REACTIVE — the degenerate single-node blueprint.
    DYNAMIC — grown at runtime by a Command (goto/send), checked at the gate.
    """

    STATIC = "static"
    CONDITIONAL = "conditional"
    PLANNED = "planned"
    REACTIVE = "reactive"
    DYNAMIC = "dynamic"


@dataclass(frozen=True)
class Node:
    """One step of the blueprint: what to run and what it waits for.

    Static only — status/output/attempts live on the run's
    :class:`NodeRuntimeState`. Frozen: a Node is shared by every run of the
    plan and referenced across replan versions, so nothing about it may
    change after construction.

    The body is one of the five kinds (:mod:`prodagent.kernel.bodies`) —
    function, tool, single model call, autonomous loop, or child agent —
    and ``action`` is its display/resolution name, kept as a property so
    events and hooks speak one vocabulary.
    """

    node_id: str
    body: NodeBody
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: Sequence[str] = ()
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
        """The body's target name — the one string events, hooks, and the
        wire all use for "what this node invokes"."""
        return self.body.target

    @property
    def kind(self) -> NodeKind:
        return self.body.kind

    def to_hook_dict(self, *, include_terminal: bool = False) -> dict[str, Any]:
        """Hook/event form: only what an observer needs. Slimmer than the
        checkpoint form on purpose — hooks fire per node, payload bytes add up."""
        d: dict[str, Any] = {
            "id": self.node_id,
            "action": self.action,
            "kind": self.kind.value,
            "params": dict(self.params),
            "depends_on": list(self.depends_on),
        }
        if include_terminal:
            d["terminal"] = self.is_terminal
        return d


def node_wire_dict(node: Node, state: NodeRuntimeState) -> dict[str, Any]:
    """Checkpoint/event form of a node — static fields from the blueprint
    (kind, target, body extras), progress fields from the run's state. One
    dict, both halves, because the durable wire predates the split and
    stays stable across it."""
    return {
        "node_id": node.node_id,
        "kind": node.kind.value,
        "action": node.action,
        "origin": node.origin.value,
        **body_to_wire_extras(node.body),
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


def react_plan(plan_id: str | None = None) -> Plan:
    """REACTIVE's degenerate blueprint: one autonomous node, no edges — the
    entire loop lives inside the body (column 4's 退化统一)."""
    plan = Plan(plan_id=plan_id, origin=Origin.REACTIVE)
    plan.add_nodes(
        [Node(node_id="react", body=ReActBody(), is_terminal=True, origin=Origin.REACTIVE)]
    )
    return plan


def default_validator() -> PlanValidator:
    """The shared five-check gate — lazily resolved so dag and validator can
    cite each other's types without an import cycle."""
    from prodagent.plan.ir.validator import PlanValidator

    return PlanValidator()


def fresh_states(plan: Plan) -> dict[str, NodeRuntimeState]:
    """All-PENDING states for a plan — the starting point of a new run."""
    return {n.node_id: NodeRuntimeState(n.node_id) for n in plan.nodes.values()}


def state_of(states: Mapping[str, NodeRuntimeState], node_id: str) -> NodeRuntimeState:
    """Read a node's state, vacuously PENDING when untouched so far."""
    st = states.get(node_id)
    return st if st is not None else NodeRuntimeState(node_id)


class Plan:
    """Versioned DAG of frozen Nodes — the blueprint a run executes."""

    def __init__(self, plan_id: str | None = None, *, origin: Origin = Origin.PLANNED) -> None:
        self.plan_id = plan_id or new_uuid4()
        self.origin = origin
        self.version: int = 1
        self._nodes: dict[str, Node] = {}
        # Reverse index (dependency -> dependents) so "what becomes ready
        # when this node lands" is a lookup, not a graph rescan.
        self._dependents: dict[str, list[str]] = {}
        self.task_input: str = ""

    # ── Construction ────────────────────────────────────────────────────────

    def add_nodes(self, nodes: list[Node]) -> None:
        """Initial insertion (planner output / Workflow compile). Pure
        construction: shape checks live in the
        :class:`~prodagent.plan.ir.validator.PlanValidator` — every birth
        line (compiler, planner, merge) validates before calling this."""
        for n in nodes:
            self._nodes[n.node_id] = n
            self._index_deps(n)

    def derive(self, *, plan_id: str, task_input: str) -> Plan:
        """Same blueprint, new identity — how a reusable Workflow template
        becomes this run's plan without copying (nodes are frozen and shared)."""
        derived = Plan(plan_id=plan_id, origin=self.origin)
        derived.version = self.version
        derived.task_input = task_input
        derived._nodes = dict(self._nodes)
        derived._reindex()
        return derived

    def _reindex(self) -> None:
        self._dependents = {}
        for n in self._nodes.values():
            self._index_deps(n)

    def _index_deps(self, node: Node) -> None:
        for dep_id in node.depends_on:
            self._dependents.setdefault(dep_id, []).append(node.node_id)

    # ── Reads (progress comes in as an argument) ───────────────────────────

    @property
    def nodes(self) -> dict[str, Node]:
        return dict(self._nodes)

    def get_node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def ready(
        self,
        states: Mapping[str, NodeRuntimeState],
        unlocked: frozenset[str] = frozenset(),
    ) -> list[Node]:
        """PENDING nodes whose dependencies are all COMPLETED — the wave the
        executor fans out concurrently. Read-only tools racing inside a node
        is a lower-level concern; *this* is the DAG's parallelism unit.

        ``unlocked`` carries the dynamic edges: dependencies a Goto has lit
        up count as satisfied even while their node's static state says
        otherwise."""
        return [
            n
            for n in self._nodes.values()
            if state_of(states, n.node_id).status is NodeStatus.PENDING
            and self._deps_done(n, states, unlocked)
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
        unlocked: frozenset[str] = frozenset(),
    ) -> bool:
        for dep_id in node.depends_on:
            if dep_id in unlocked:
                continue  # a dynamic edge satisfies this dependency by decree
            dep = self._nodes.get(dep_id)
            if dep is None:
                raise ValueError(
                    f"Node {node.node_id!r} references unknown dependency {dep_id!r}. "
                    "Plan blueprint is corrupted."
                )
            if state_of(states, dep_id).status is not NodeStatus.COMPLETED:
                return False
        return True

    # ── Replan — new version, never in-place mutation ──────────────────────

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
            for dep_id in nn.depends_on:
                if (
                    dep_id in self._nodes
                    and state_of(states, dep_id).status is NodeStatus.OBSOLETE
                    and dep_id not in new_ids
                ):
                    raise ValueError(
                        f"Node {nn.node_id!r}: dependency {dep_id!r} is OBSOLETE "
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
        default_validator().validate([*live, *new_nodes])

        merged = Plan(plan_id=self.plan_id, origin=self.origin)
        merged.version = self.version + 1  # every replan advances the version — lineage
        merged.task_input = self.task_input
        merged._nodes = dict(self._nodes)
        for nn in new_nodes:
            stamped = replace(nn, version_created=merged.version)
            if stamped.replaces_node_id:
                old_state = states.get(stamped.replaces_node_id)
                if old_state is not None:
                    old_state.mark_obsolete()  # replaced, not deleted — history replays
            merged._nodes[stamped.node_id] = stamped
        merged._reindex()
        return merged

    def sprout(self, instances: list[Node], *, rewire_from: str) -> None:
        """Grow dynamic instances into the graph and re-wire ``rewire_from``'s
        downstream onto the whole batch — the Send application. The join is the
        barrier: downstream nodes now wait for every instance to reach a
        terminal state. Dynamic growth carries DYNAMIC lineage; the version
        advances so the event log replays every revision."""

        def replace_dep(n: Node) -> Node | None:
            if rewire_from not in n.depends_on:
                return None
            return replace(
                n,
                depends_on=tuple(d for d in n.depends_on if d != rewire_from)
                + tuple(i.node_id for i in instances),
            )

        rewired = [rn for n in self._nodes.values() if (rn := replace_dep(n)) is not None]
        for n in rewired:
            self._nodes[n.node_id] = n
        for inst in instances:
            self._nodes[inst.node_id] = inst
            self._index_deps(inst)
        self._reindex()
        self.version += 1

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
            plan._nodes[nid] = Node(
                node_id=nid,
                body=body_from_wire(nd.get("kind", ""), nd.get("action", ""), nd),
                params=nd.get("params", {}),
                depends_on=nd.get("depends_on", []),
                is_terminal=nd.get("is_terminal", False),
                origin=Origin(nd.get("origin", Origin.PLANNED.value)),
                version_created=nd.get("version_created", plan.version),
                replaces_node_id=nd.get("replaces_node_id"),
            )
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
        plan._reindex()
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
        state), right before the node runs — data flows along the DAG's
        edges without the executor knowing any wiring."""
        result = _resolve(node.params, node, self, states, shared or {})
        return result if isinstance(result, dict) else {}


_TEMPLATE_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_LOOSE_TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")


def _resolve(
    value: Any,
    node: Node,
    plan: Plan,
    states: Mapping[str, NodeRuntimeState],
    shared: Mapping[str, Any],
) -> Any:
    """Recursive template expansion over params. Whole-string refs preserve
    the referenced value's type (a dict stays a dict); embedded refs must
    stringify. Unknown-but-template-looking syntax is a hard error with the
    offender named — silent passthrough would surface much later as a tool
    receiving the literal string ``"{{x.output}}"``."""
    if isinstance(value, Mapping):
        return {k: _resolve(v, node, plan, states, shared) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, node, plan, states, shared) for v in value]
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
        return _lookup(matches[0].group(1), node, plan, states, shared)
    return _TEMPLATE_RE.sub(lambda m: str(_lookup(m.group(1), node, plan, states, shared)), value)


def _lookup(
    ref: str,
    node: Node,
    plan: Plan,
    states: Mapping[str, NodeRuntimeState],
    shared: Mapping[str, Any],
) -> Any:
    """Resolve one ``{{...}}`` reference against the plan's completed nodes.

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
        return plan.task_input

    if rest and rest[0] == "output":
        rest = rest[1:]
    dep = plan.get_node(dep_id)
    if dep is None:
        raise ValueError(
            f"param chain: node {node.node_id!r} references {dep_id!r} which is not in the plan"
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
