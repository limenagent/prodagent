"""kernel — the engine: vocabulary, topology, units, state, execution.

Modules, one concept each: ``unit`` is the one composable interface
(Unit/Outcome/control — everything runnable is a Unit) with its service
slots; ``units`` the five built-in units a node can run; ``combinators``
the four composition shapes (Sequential/Parallel/Route/Loop, Units
themselves); ``graph`` pure topology (Graph/Plan/Edge) with
``graph_validator`` the five-check birth gate and ``command`` the
state-write primitive (Update); ``run`` the run state (typed
SchedulerCursor, park/resume vocabulary); ``node_state`` how far a run has
gotten with each node; ``scheduler`` the one engine (waves, bootstrap,
graph event log, node runner, finalize); ``budget`` the ceiling and the
shared ledger; ``bus`` the one seam to the outside; ``progress`` the
dead-loop guard.

No modes, no execution-strategy enums: a run's shape — the agent itself
as a unit, a preset graph, a drafted one — is a composition decision made
above the kernel and injected (PlannerPort, AutonomyEngine). Nothing here
imports a capability package; the kernel's purity law is CI-tested.
"""
