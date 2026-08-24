"""kernel — the reading unit: vocabulary, state, budget, bus, step.

These six modules are the irreducible core every layer builds on and none
may import a capability package. ``types``/``events``/``state`` are the
nouns; ``budget`` is the ceiling and the shared ledger; ``bus`` is the one
seam to the outside; ``step`` is the atom of agency — one model call plus
at most one tool round.
"""
