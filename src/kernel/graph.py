"""graph — the static blueprint: nodes, edges, Plan, and the "who is ready
this wave" computation.

The blueprint answers "which steps exist, how they connect, what state looks
like". It holds no dynamic data of any single run (how far it got, current
state) — that belongs to Run, a core separation. One Plan can be reused by
many concurrent Runs.

The only slightly "dynamic" thing here is ready(): given a Run's current
state, it computes which nodes can run next. It is a pure read that mutates
nothing, which makes the scheduling logic very easy to test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.kernel.channels import Channel

# Conditional-edge predicate: looks only at current shared state and decides
# whether the edge is "live".
EdgePredicate = Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class Edge:
    """A directed edge. ``when=None`` means always live; otherwise it is
    activated according to current state."""

    source: str
    target: str
    when: EdgePredicate | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """Step-level resilience policy (the mechanism only executes; whether to
    retry and how long to back off are replaceable configuration).

    - max_attempts: maximum attempts including the first;
    - base_delay/factor: exponential backoff; after the n-th failure wait
      base_delay * factor**n;
    - retry_on: retry only these exceptions, or a predicate for finer control
      (e.g. retry a 503 but not a 400); external cancellation
      (CancelledError) is never retried.
    """

    max_attempts: int = 3
    base_delay: float = 0.05
    factor: float = 2.0
    retry_on: tuple[type, ...] | Callable[[BaseException], bool] = (Exception,)

    def delay_for(self, failed_attempt: int) -> float:
        # failed_attempt starts at 0: after the first failure wait base_delay,
        # after the second base_delay*factor.
        return self.base_delay * (self.factor**failed_attempt)

    def matches(self, exc: BaseException) -> bool:
        if isinstance(self.retry_on, tuple):
            return isinstance(exc, self.retry_on)
        return bool(self.retry_on(exc))


@dataclass(frozen=True)
class Node:
    """A step in the graph.

    - body: the composable unit that does the work (see body.py); the blueprint
      does not care what is inside it;
    - join: with multiple predecessors, "all" = ready when all live edges are
      done, "any" = ready when any one is done; a callable ``(done, total) ->
      bool`` is an escape hatch for custom quorum policies. Plan.validate()
      rejects anything else (typos included) at build time;
    - template: it is a "dynamic fan-out template", instantiated multiple times
      at runtime via Send;
    - terminal: marks the convergence node that produces the final result;
    - timeout: max seconds for one execution; a timeout counts as one failure;
    - retry: retry/backoff policy after failure; absent means run once and fail.
    """

    id: str
    body: Any
    join: str | Callable[[int, int], bool] = "all"
    template: bool = False
    terminal: bool = False
    timeout: float | None = None
    retry: RetryPolicy | None = None


@dataclass
class Plan:
    """A static execution blueprint = node table + edges + declared state
    channels + entry."""

    channels: dict[str, Channel] = field(default_factory=dict)
    entry: tuple[str, ...] = ()
    _nodes: dict[str, Node] = field(default_factory=dict, init=False)
    _incoming: dict[str, list[Edge]] = field(default_factory=dict, init=False)
    _outgoing: dict[str, list[Edge]] = field(default_factory=dict, init=False)

    # — build time: chained declarations, validated once after building —
    def add(self, *nodes: Node) -> Plan:
        for n in nodes:
            if n.id in self._nodes:
                raise ValueError(f"duplicate node id: {n.id}")
            self._nodes[n.id] = n
            self._incoming.setdefault(n.id, [])
            self._outgoing.setdefault(n.id, [])
        return self

    def edge(self, source: str, target: str, when: EdgePredicate | None = None) -> Plan:
        e = Edge(source, target, when)
        self._outgoing.setdefault(source, []).append(e)
        self._incoming.setdefault(target, []).append(e)
        return self

    def validate(self) -> Plan:
        """Compile-time health check: dangling edges, unknown entries, and
        malformed join policies fail here, not halfway through a run."""
        known = set(self._nodes)
        for src, outs in self._outgoing.items():
            if src not in known:
                raise ValueError(f"edge source {src!r} does not exist")
            for e in outs:
                if e.target not in known:
                    raise ValueError(f"edge {src}->{e.target} target does not exist")
        for en in self.entry:
            if en not in known:
                raise ValueError(f"entry node {en!r} does not exist")
        for nid, node in self._nodes.items():
            if not callable(node.join) and node.join not in ("all", "any"):
                raise ValueError(
                    f"node {nid!r} has invalid join {node.join!r}; "
                    "expected 'all', 'any', or a callable(done, total) -> bool"
                )
        if not self.entry:
            # With no explicit entry, treat nodes that have no incoming edge as entries.
            self.entry = tuple(nid for nid in known if not self._incoming[nid])
        return self

    # — read access —
    @property
    def nodes(self) -> dict[str, Node]:
        return self._nodes

    def get(self, node_id: str) -> Node:
        return self._nodes[node_id]

    def incoming(self, node_id: str) -> list[Edge]:
        return list(self._incoming.get(node_id, ()))

    def initial_shared(self) -> dict[str, Any]:
        """Each channel starts from its declared initial value."""
        return {k: ch.init for k, ch in self.channels.items()}

    def terminal_ids(self) -> list[str]:
        marked = [nid for nid, n in self._nodes.items() if n.terminal]
        if marked:
            return marked
        # With no terminal marked, nodes without an outgoing edge are the end.
        return [nid for nid in self._nodes if not self._outgoing[nid]]

    # — predecessor checks —
    @staticmethod
    def _edge_live(e: Edge, shared: dict[str, Any]) -> bool:
        return e.when is None or bool(e.when(shared))

    def _predecessor_done(self, run: Any, source: str, *, empty_fanout: bool = False) -> bool:
        """A predecessor "completed successfully": a normal node is COMPLETED;
        a template needs all its instances in a terminal state."""
        node = self._nodes[source]
        if not node.template:
            return run.is_completed(source)
        instances = run.instances.get(source, ())
        if not instances:
            return empty_fanout
        return all(run.is_terminal(k) for k in instances)

    def _predecessor_terminal(self, run: Any, source: str, *, empty_fanout: bool = False) -> bool:
        """Whether the predecessor reached any terminal state (done/skipped/failed)."""
        node = self._nodes[source]
        if not node.template:
            return run.is_terminal(source)
        instances = run.instances.get(source, ())
        if not instances:
            return empty_fanout
        return all(run.is_terminal(k) for k in instances)

    # — core: who is ready this wave —
    def ready(self, run: Any, *, empty_fanout: bool = False) -> list[str]:
        """Pure computation of the node/instance ids that can run right now.

        When a conditional edge is "not live at this moment" but its predecessor
        is not terminal yet, we just "wait a little longer", never declare it
        dead early — the predecessor may change state next wave and make the
        edge live (this is exactly the ReAct back-edge). Truly dead branches are
        cleaned up by sweep_skipped once the whole graph stalls.
        """
        ready: list[str] = []
        # A template node itself is never scheduled; it only runs through the
        # instances produced by Send.
        static_keys = [nid for nid, n in self._nodes.items() if not n.template]
        all_keys = static_keys + [k for ks in run.instances.values() for k in ks]
        for key in all_keys:
            if not run.is_pending(key):
                continue
            if run.is_instance(
                key
            ):  # dynamic instance: Send activated it directly, pending is enough to run
                ready.append(key)
                continue
            preds = self._incoming.get(key, ())
            # A start: an explicit entry, or a node explicitly activated by a Goto
            # command — pending is enough to start.
            if key in self.entry or key in run.activated:
                ready.append(key)
                continue
            if not preds:
                # Under an explicit entry, a node with no incoming edge that is
                # neither an entry nor command-activated does not auto-start; it
                # waits for a Goto/Send, so an isolated node isn't mistaken for a
                # source on the first wave.
                continue

            has_open_pred = any(
                not self._predecessor_terminal(run, e.source, empty_fanout=empty_fanout)
                for e in preds
            )
            if has_open_pred:
                continue  # a predecessor is still running, wait another wave

            live_edges = [e for e in preds if self._edge_live(e, run.shared)]
            if not live_edges:
                continue  # no live edge; sweep_skipped cleans this up once the graph stalls
            done = sum(
                1
                for e in live_edges
                if self._predecessor_done(run, e.source, empty_fanout=empty_fanout)
            )
            if done == 0:
                continue
            if not self._join_satisfied(key, done, len(live_edges)):
                continue
            ready.append(key)
        return ready

    def _join_satisfied(self, key: str, done: int, total: int) -> bool:
        """any: the first live-and-done edge is enough; all: every live edge
        must be; a callable gets (done, total) for a custom quorum policy."""
        join = self._nodes[key].join
        if callable(join):
            return bool(join(done, total))
        return done >= total if join == "all" else True

    def sweep_skipped(self, run: Any) -> None:
        """Dead-branch cleanup: mark pending nodes whose predecessors are all
        terminal but which have no live edge as skipped. Cascades to a fixed
        point. Called only when "no node is ready this wave", so it cannot kill
        a branch that may be activated later (e.g. one driven by a back-edge)."""
        static_keys = [nid for nid, n in self._nodes.items() if not n.template]
        changed = True
        while changed:
            changed = False
            for key in static_keys:
                if not run.is_pending(key) or key in self.entry:
                    continue
                preds = self._incoming.get(key, ())
                if not preds:
                    continue
                all_terminal = all(self._predecessor_terminal(run, e.source) for e in preds)
                any_live = any(
                    self._predecessor_done(run, e.source) and self._edge_live(e, run.shared)
                    for e in preds
                )
                if all_terminal and not any_live:
                    run.mark_skipped(key)
                    changed = True

    def is_done(self, run: Any) -> bool:
        """All (non-template) static nodes and dynamic instances reached a
        terminal state (done/skipped/failed)."""
        static_keys = [nid for nid, n in self._nodes.items() if not n.template]
        keys = static_keys + [k for ks in run.instances.values() for k in ks]
        return all(run.is_terminal(k) for k in keys)
