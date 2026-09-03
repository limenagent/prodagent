"""prodagent.coordination — multi-agent collaboration, two primitives.

Mapped to Anthropic's coordination-pattern taxonomy:

- ``spawn``  delegate — parent delegates to children (orchestrator-subagent,
             call-return: the parent keeps control while awaiting the result).
- ``peer``   relay — control transfers along a chain, point-to-point (handoff:
             the sender's run ends, the peer's run begins).

Board-shaped collaboration (experts opportunistically writing a shared
workspace) is deliberately not a primitive: it composes from Route (a
selector reading the full state) inside Loop — a recipe over the graph
atoms, not a class (removed 2026-09-02; see REFACTOR-PLAN.md).

Modules:

- ``spawn`` / ``peer`` the two delegation strategies
- ``activation``  the one activation core every delegation path shares
- ``settle``      chain-terminal discipline (output contract, checkpoint,
                  RUN_COMPLETE gate)
- ``messaging``   governance pipeline every crossing flows through
                  (dedupe → contract → gate → audit)
"""
