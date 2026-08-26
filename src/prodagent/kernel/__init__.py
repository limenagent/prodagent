"""kernel — the reading unit: vocabulary, state, budget, bus, step, loop, guard.

Seven modules, one concept each: ``types`` is the nouns (calls, responses,
results, stream events); ``state`` is the run; ``budget`` is the ceiling and
the shared ledger; ``bus`` is the one seam to the outside (tri-protocol,
with its dispatch plumbing); ``step`` is the atom of agency — one model call
plus at most one tool round; ``loop`` is the policy for iterating steps;
``progress`` is the dead-loop guard. None may import a capability package.
"""
