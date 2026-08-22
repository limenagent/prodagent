"""prodagent.coordination — multi-agent coordination primitives.

Five primitives, two axes, one plane:

- **Primitives** — ``spawn`` (``agents=``, vertical delegation), ``peer`` +
  ``run_loop`` (``peers=``, horizontal relay), ``ensemble`` (shared floor),
  ``blackboard`` (versioned board), ``work_queue`` (lease-based queue).
- **Axes** — each primitive is a ``shared-state × activation`` combination:
  what the shared store's write semantics are (``_store``) and who gets
  activated next (``activation``). Budget ceilings are shared via
  ``budget_ledger``, liveness via ``termination``.
- **Plane** — whatever the topology, collaborating means messages *crossing
  agent boundaries*. Every crossing in every primitive flows through
  :mod:`.messaging` as a ``Crossing`` envelope on one of two pipelines:
  ``assembly_pipeline`` (DOWNSTREAM — assembled toward a consuming context;
  the container is the whitelist) or ``admission_pipeline`` (UPSTREAM — a
  producing agent's output entering shared state; contract + gate + dead
  letter at the boundary). Sanitization, identity, observability, and
  security veto ride the same crossings — mounted once, inherited by every
  primitive.
"""
