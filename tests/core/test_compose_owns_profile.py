"""``profile`` is owned by compose — no other module branches on it.

Every "what does production() turn on" decision lives in
``runtime/compose.py`` (the consumer side) and ``core/config.py`` (the flag
side). The hook bundles decide attach-order by profile once, in their own
base — that is bundle selection, not feature wiring, and is allowed.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "prodagent"

ALLOWED = {
    Path("runtime/compose.py"),
    Path("core/config.py"),
    Path("hooks/bundles/base.py"),
}


def test_no_profile_branches_outside_compose() -> None:
    offenders: list[str] = []
    for p in SRC.rglob("*.py"):
        if p.relative_to(SRC) in ALLOWED or "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8")
        if 'profile == "' in text or 'profile != "' in text:
            offenders.append(str(p.relative_to(SRC)))
    assert not offenders, (
        "profile branching leaked outside compose/config:\n  " + "\n  ".join(offenders)
    )
