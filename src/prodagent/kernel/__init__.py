"""kernel — the engine: vocabulary, topology, bodies, state, execution.

Modules, one concept each: ``body`` is the one composable interface
(NodeBody/Outcome — everything runnable is a body) with its service
slots; ``bodies`` the built-in bodies a node can run; ``graph`` pure topology (Graph/Plan/Edge —
``Plan(nodes=[...])`` is the one way a blueprint comes into existence);
``command`` the control vocabulary (Update/Goto/Send/Handoff — what a
node's return asks the kernel to do); ``run`` the run state (typed
SchedulerCursor, the approval park); ``node_state`` how far a run has
gotten with each node; ``bare`` the in-process IO pair (the bare-profile
default); ``scheduler`` the one engine (waves, bootstrap, graph event
log, node runner, finalize); ``budget`` the ceiling and the shared
ledger; ``bus`` the one seam to the outside.

Reading order is build order: ``body`` → ``bodies`` → ``graph`` →
``command``/``channels`` → ``run``/``node_state``/``interrupt`` →
``event_log`` → ``node_runner`` → ``bootstrap`` → ``scheduler``/``finalize``.
The constraint seams (``budget``, ``bus``, ``bare``) wrap around them.
Loop policies (the dead-loop guard, ``runtime/progress.py``) live with the
loop in runtime — the kernel carries vocabulary and engine, nothing else.

No modes, no execution-strategy enums: a run's shape — the agent itself
as a unit, a preset graph, a drafted one — is a composition decision made
above the kernel and injected (PlannerPort, the wiring bag's services).
Nothing here
imports a capability package; the kernel's purity law is CI-tested.
"""
