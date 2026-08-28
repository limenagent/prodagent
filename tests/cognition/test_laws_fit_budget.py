"""fit_within_budget laws — the spec of "keep the longest tail that fits".

For arbitrary items, budgets, and token costs, the result must be:

1. a **suffix** of the input (recency wins — the oldest entries are cut);
2. **within budget** (including separators between kept items);
3. **maximal**: if anything was dropped from the front, adding back the
   next-left item would overflow — the cut is the greedy minimum, so
   compression stages never sacrifice more history than the budget forces.

Compressors, recall pruning, and emergency truncation all route through
this one function; this law is what makes "bounded view" mean the same
thing at every call site.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from prodagent.cognition.context.budget import fit_within_budget

_items = st.lists(st.text(min_size=0, max_size=20), max_size=12)
_budgets = st.integers(min_value=0, max_value=60)
_seps = st.integers(min_value=0, max_value=3)


def _token_of(text: str) -> int:
    return len(text)


@settings(max_examples=200, deadline=None)
@given(_items, _budgets, _seps)
def test_fit_within_budget_law(items: list[str], budget: int, sep: int) -> None:
    kept = fit_within_budget(items, budget, _token_of, separator_tokens=sep)

    # 1. suffix
    if kept:
        assert kept == items[-len(kept) :]

    # 2. within budget
    cost = sum(_token_of(t) for t in kept) + sep * max(0, len(kept) - 1)
    assert cost <= budget

    # 3. maximal: a non-empty cut means the next-left item would not fit
    dropped = len(items) - len(kept)
    if dropped > 0:
        next_left = items[len(items) - len(kept) - 1]
        extra = _token_of(next_left) + (sep if kept else 0)
        assert cost + extra > budget
