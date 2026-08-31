"""prodagent.coordination — multi-agent collaboration, three primitives.

Mapped to Anthropic's coordination-pattern taxonomy:

- ``spawn``      delegate — parent delegates to children (orchestrator-subagent)
- ``peer``       relay — control transfers along a chain (point-to-point)
- ``blackboard`` board — experts write versioned slots as triggers fire
                 (shared state; covers agent-team via persistent workers)

Two subpackages:

- ``infra``    the machinery around the loops (dispatch, termination, budget)
- ``messaging`` governance pipeline every crossing flows through
               (dedupe → contract → gate → audit)

These are presets, not a cage — the atoms underneath are public.
"""
