#!/usr/bin/env python3
"""Check that tracked abstractions actually have production callers.

This repo's recurring defect isn't missing abstractions — it's abstractions
that get designed, written, fully tested, and then never wired to the call
sites they were meant to replace ("ghost abstractions": ActivationPolicy sat
dead for months while a package docstring called activation an axis).

MANIFEST below is the explicit, hand-curated list of symbols this applies to.
Each entry has a `status`:

- "orphan": known half-wired abstraction, currently below `min_hits` on
  purpose (tracked debt). Failing to meet `min_hits` is expected and does
  NOT fail CI — but exceeding it prints a hint to flip the status once the
  wiring lands.
- "wired": the abstraction has been connected to its real caller(s). CI FAILS
  if hits drop below `min_hits` — this is the regression guard. Do not add an
  entry as "wired" without confirming the caller exists outside tests/.

Usage: python scripts/check_orphan_abstractions.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "prodagent"


@dataclass(frozen=True)
class Entry:
    name: str
    pattern: str  # regex, matched against file contents
    defining_file: str  # repo-relative path; never counted as its own caller
    min_hits: int
    status: str  # "orphan" | "wired"
    note: str  # where it should be wired, for humans reading a failure
    scope_file: str | None = None  # if set, only this repo-relative file is searched


MANIFEST: list[Entry] = [
    # kernel.budget.evaluate_axes likewise left the manifest when its module
    # merged into kernel/budget.py: both callers (check_budget and the
    # BudgetLedger) live in the same file it is defined in.
    # kernel.bus.Pipeline was removed from this manifest when pipeline.py
    # merged into bus.py: the plumbing is now module-internal to its only
    # consumer (HookRegistry), so there is no cross-module wiring to lose.
    # coordination.activation.ActivationPolicy left when blackboard (its only
    # implementor and consumer) was removed 2026-09-02; the batch-activation
    # vocabulary went with it (REFACTOR-PLAN.md U1).
]


def _count_hits(entry: Entry) -> int:
    regex = re.compile(entry.pattern)
    if entry.scope_file is not None:
        path = REPO / entry.scope_file
        return len(regex.findall(path.read_text(encoding="utf-8"))) if path.exists() else 0

    defining = REPO / entry.defining_file
    hits = 0
    for py in SRC.rglob("*.py"):
        if py == defining:
            continue
        text = py.read_text(encoding="utf-8")
        hits += len(regex.findall(text))
    return hits


def main() -> int:
    failures: list[str] = []
    for entry in MANIFEST:
        hits = _count_hits(entry)
        met = hits >= entry.min_hits
        if entry.status == "wired":
            if not met:
                failures.append(
                    f"REGRESSION  {entry.name}: expected >= {entry.min_hits} "
                    f"caller(s), found {hits}. This was wired — something "
                    f"un-wired it. {entry.note}"
                )
            else:
                print(f"wired   {entry.name}: {hits} caller(s), OK")
        else:  # orphan
            if met:
                print(
                    f"READY   {entry.name}: {hits} caller(s) found — this "
                    f"orphan looks fixed. Flip status to 'wired' in "
                    f"{Path(__file__).name}. {entry.note}"
                )
            else:
                print(f"orphan  {entry.name}: {hits} caller(s) (tracked debt). {entry.note}")

    if failures:
        for f in failures:
            print(f"\nFAIL {f}")
        return 1
    print(f"\n{len(MANIFEST)} tracked abstraction(s) checked, no regressions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
