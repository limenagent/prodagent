"""``profile`` is owned by config + factory — no other module branches on it.

Every "what does production() turn on" decision lives in exactly two homes:
``base/config.py`` (the flag side: production() flips config fields) and
``backends/factory.py`` (the service side: profile-scoped wrappers like the
response cache and the blob store). The hook bundles decide attach-order by
profile once, in their own base — that is bundle selection, not feature
wiring, and is allowed.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "prodagent"

ALLOWED = {
    Path("base/config.py"),
    Path("backends/factory.py"),
    Path("hooks/bundles/base.py"),
}


def test_no_profile_branches_outside_config_and_factory() -> None:
    offenders: list[str] = []
    for p in SRC.rglob("*.py"):
        if p.relative_to(SRC) in ALLOWED or "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8")
        if 'profile == "' in text or 'profile != "' in text:
            offenders.append(str(p.relative_to(SRC)))
    assert not offenders, (
        "profile branching leaked outside config/factory/bundles:\n  " + "\n  ".join(offenders)
    )
