"""prodagent.coordination — multi-agent collaboration, one file per style.

Any collaboration answers three questions — **who with whom** (the topology),
**who acts next** (activation), **how results cross a boundary** (the message
plane) — plus two cross-cutting concerns: **when to stop** (termination +
shared budget) and **what survives a crash** (event sourcing).

The top level is exactly the five topologies, one self-contained file each
(spec + shared state + driver + member adapter + events):

- ``spawn``      delegate — a parent delegates to a child and gets a result back
                (``agents=``; the model calls ``spawn_agent``).
- ``peer``      relay — control transfers along a chain (``peers=``; tools +
                the relay in one place, returning a ``HandoffActivation``).
- ``ensemble``  debate — members debate on one shared floor (transcript,
                projection, speaking orders, event-sourced resume).
- ``blackboard`` board — experts write versioned slots as triggers fire
                (optimistic versions, buzz-in or fan-out dispatch).
- ``work_queue`` queue — workers claim items under lease; retry and dead
                letter are the queue's.

Two subpackages, two different axes:

- ``infra``    the machinery around the loops, owned by no single style —
               dispatch, termination, the budget envelope, durable-store
               mechanics, the model-facing stage tools, chain-terminal
               discipline (``settle``).
- ``messaging`` the plane results cross — every crossing is a ``Crossing``
               envelope on the assembly (DOWNSTREAM) or admission (UPSTREAM)
               pipeline; dedupe, contract, trim, gate, dead letter mounted
               once and inherited by every primitive.

The round *bodies* are deliberately not unified — an ensemble picks a
speaker, a blackboard matches triggers, a queue sweeps leases; forcing them
identical would erase real semantics. What unifies is everything around them.

These five are presets, not a cage. The atoms underneath are public —
``infra.store`` (``SharedStore`` / ``EventSourcedStore``), ``infra.stage``
(``StageDriver`` / ``TerminationPolicy`` with the ``AllPass`` /
``BoardSatisfied`` / ``Drained`` business strategies), ``ports.activation``
(``ActivationPolicy``), ``messaging`` (the Crossing pipelines), the budget
envelope — and a sixth topology is a ``StageDriver`` subclass away.
``tests/runtime/test_composition_custom_topology.py`` builds one (a
Board × round-robin review loop, a grid cell none of the five covers) from
public atoms alone; treat it as the worked recipe.
"""
