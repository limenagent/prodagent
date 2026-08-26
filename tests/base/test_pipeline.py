from __future__ import annotations

import asyncio
import time

import pytest

from prodagent.kernel.bus import _Pipeline as Pipeline
from prodagent.kernel.bus import _Stage as Stage
from prodagent.kernel.bus import _StageMode as StageMode


class _Veto(Exception):
    """Stand-in for a structured security-veto exception in these tests."""


def _is_veto(exc: BaseException) -> bool:
    return isinstance(exc, _Veto)


@pytest.mark.asyncio
async def test_observe_runs_stages_concurrently():
    pipeline = Pipeline()
    seen: list[str] = []

    async def slow(tag: str) -> None:
        await asyncio.sleep(0.05)
        seen.append(tag)

    pipeline.mount("point", Stage(name="a", fn=lambda: slow("a"), mode=StageMode.OBSERVE))
    pipeline.mount("point", Stage(name="b", fn=lambda: slow("b"), mode=StageMode.OBSERVE))
    pipeline.mount("point", Stage(name="c", fn=lambda: slow("c"), mode=StageMode.OBSERVE))

    start = time.monotonic()
    await pipeline.observe("point", {})
    elapsed = time.monotonic() - start

    assert set(seen) == {"a", "b", "c"}
    assert elapsed < 0.12, f"expected concurrent ~50ms, got {elapsed * 1000:.0f}ms"


@pytest.mark.asyncio
async def test_observe_swallows_non_veto_exceptions():
    pipeline = Pipeline()
    ran: list[str] = []

    def bad() -> None:
        raise RuntimeError("boom")

    def good() -> None:
        ran.append("good")

    pipeline.mount("point", Stage(name="bad", fn=bad, mode=StageMode.OBSERVE))
    pipeline.mount("point", Stage(name="good", fn=good, mode=StageMode.OBSERVE))

    await pipeline.observe("point", {}, is_veto=_is_veto)

    assert ran == ["good"]


@pytest.mark.asyncio
async def test_observe_reraises_structured_veto():
    pipeline = Pipeline()

    def raises_veto() -> None:
        raise _Veto("nope")

    pipeline.mount("point", Stage(name="veto", fn=raises_veto, mode=StageMode.OBSERVE))

    with pytest.raises(_Veto):
        await pipeline.observe("point", {}, is_veto=_is_veto)


@pytest.mark.asyncio
async def test_gather_runs_concurrently_and_collects_non_none_in_priority_order():
    pipeline = Pipeline()

    async def slow_high() -> str:
        await asyncio.sleep(0.05)
        return "high"

    async def fast_low() -> str:
        return "low"

    pipeline.mount("point", Stage(name="high", fn=slow_high, mode=StageMode.GATHER, priority=100))
    pipeline.mount("point", Stage(name="low", fn=fast_low, mode=StageMode.GATHER, priority=10))

    start = time.monotonic()
    results = await pipeline.gather("point", {})
    elapsed = time.monotonic() - start

    assert results == ["high", "low"]
    assert elapsed < 0.09, f"expected concurrent ~50ms, got {elapsed * 1000:.0f}ms"


@pytest.mark.asyncio
async def test_gather_filters_none_and_invokes_on_error():
    pipeline = Pipeline()
    errors: list[str] = []

    async def failing() -> None:
        raise RuntimeError("bad")

    async def ok() -> str:
        return "ok"

    async def on_error(stage: Stage, exc: Exception) -> None:
        errors.append(f"{stage.name}:{exc}")

    pipeline.mount("point", Stage(name="failing", fn=failing, mode=StageMode.GATHER))
    pipeline.mount("point", Stage(name="ok", fn=ok, mode=StageMode.GATHER))

    results = await pipeline.gather("point", {}, is_veto=_is_veto, on_error=on_error)

    assert results == ["ok"]
    assert errors == ["failing:bad"]


@pytest.mark.asyncio
async def test_veto_short_circuits_on_first_interpreted_stop():
    pipeline = Pipeline()
    ran_second: list[bool] = []

    def first(**_: object) -> str:
        return "blocked"

    def second(**_: object) -> None:
        ran_second.append(True)

    pipeline.mount("point", Stage(name="first", fn=first, mode=StageMode.VETO, priority=100))
    pipeline.mount("point", Stage(name="second", fn=second, mode=StageMode.VETO, priority=90))

    outcome = await pipeline.veto(
        "point", {}, interpret=lambda _stage, r: r if r == "blocked" else None
    )

    assert outcome == "blocked"
    assert not ran_second, "second stage must not run after a veto short-circuit"


@pytest.mark.asyncio
async def test_veto_continues_when_interpret_returns_none():
    pipeline = Pipeline()

    def passes(**_: object) -> None:
        return None

    def stops(**_: object) -> str:
        return "stop"

    pipeline.mount("point", Stage(name="passes", fn=passes, mode=StageMode.VETO, priority=100))
    pipeline.mount("point", Stage(name="stops", fn=stops, mode=StageMode.VETO, priority=90))

    outcome = await pipeline.veto(
        "point", {}, interpret=lambda _stage, r: r if r == "stop" else None
    )

    assert outcome == "stop"


@pytest.mark.asyncio
async def test_veto_returns_none_when_no_stages_stop():
    pipeline = Pipeline()
    pipeline.mount("point", Stage(name="ok", fn=lambda **_: None, mode=StageMode.VETO))

    outcome = await pipeline.veto("point", {}, interpret=lambda _stage, r: r)

    assert outcome is None


@pytest.mark.asyncio
async def test_veto_on_error_can_short_circuit_or_continue():
    pipeline = Pipeline()

    def raises(**_: object) -> None:
        raise RuntimeError("fail closed")

    def unreached(**_: object) -> None:
        raise AssertionError("must not run after on_error short-circuits")

    pipeline.mount("point", Stage(name="raises", fn=raises, mode=StageMode.VETO, priority=100))
    pipeline.mount("point", Stage(name="unreached", fn=unreached, mode=StageMode.VETO, priority=90))

    async def on_error(_stage: Stage, exc: Exception) -> str:
        return f"veto:{exc}"

    outcome = await pipeline.veto("point", {}, interpret=lambda _stage, r: r, on_error=on_error)

    assert outcome == "veto:fail closed"


@pytest.mark.asyncio
async def test_veto_reraises_structured_veto_exception():
    pipeline = Pipeline()

    def raises_veto(**_: object) -> None:
        raise _Veto("nope")

    pipeline.mount("point", Stage(name="veto", fn=raises_veto, mode=StageMode.VETO))

    with pytest.raises(_Veto):
        await pipeline.veto("point", {}, interpret=lambda _s, r: r, is_veto=_is_veto)


def test_mount_orders_by_descending_priority_stable_for_ties():
    pipeline = Pipeline()
    pipeline.mount("point", Stage(name="a", fn=lambda: None, mode=StageMode.OBSERVE, priority=0))
    pipeline.mount("point", Stage(name="b", fn=lambda: None, mode=StageMode.OBSERVE, priority=10))
    pipeline.mount("point", Stage(name="c", fn=lambda: None, mode=StageMode.OBSERVE, priority=0))

    names = [s.name for s in pipeline.stages("point")]
    assert names == ["b", "a", "c"]


def test_has_stages_false_for_unmounted_point():
    pipeline = Pipeline()
    assert not pipeline.has_stages("nothing-here")
    assert pipeline.stages("nothing-here") == []


async def _noop(**kw: object) -> None:
    return None


@pytest.mark.asyncio
async def test_mode_mismatch_is_a_wiring_error() -> None:
    pipe = Pipeline()
    pipe.mount("gate.x", Stage(name="checker", fn=_noop, mode=StageMode.VETO))
    with pytest.raises(TypeError, match="mounted as veto but dispatched as observe"):
        await pipe.observe("gate.x", {})
