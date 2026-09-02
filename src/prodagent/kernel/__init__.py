"""kernel — the reading unit: vocabulary, state, budget, bus, turn, react, guard.

Modules, one concept each: ``types`` is the nouns (calls, responses,
results, stream events); ``state`` is the run; ``node_state`` is how far a
run has gotten with each node; ``budget`` is the ceiling and the shared
ledger; ``bus`` is the one seam to the outside (tri-protocol, with its
dispatch plumbing); ``turn`` is the atom of agency — one model call plus at
most one tool round; ``react`` is the Turn loop as a node body; ``bodies``
are the five ways one node executes; ``progress`` is the dead-loop guard.
None may import a capability package — the Scheduler itself lives one
layer up, in ``plan``, where the blueprint meets hooks, models and tools.
"""
