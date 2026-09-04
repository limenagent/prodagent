"""总线三协议：旁观不影响主流程、裁决 fail-closed、收集汇总。"""

import pytest

from src.kernel import Bus


async def test_fire_observer_error_is_swallowed():
    bus = Bus()

    async def boom(**_):
        raise RuntimeError("观察者炸了")

    seen = []
    bus.on("e", boom)
    bus.on("e", lambda **kw: seen.append(kw))
    await bus.fire("e", x=1)            # 不应抛出
    assert seen == [{"x": 1}]


async def test_check_deny_wins():
    bus = Bus()
    bus.checker("gate", lambda **_: True)
    bus.checker("gate", lambda **_: False)
    verdict = await bus.check("gate")
    assert not verdict.allowed


async def test_check_fail_closed_when_checker_raises():
    bus = Bus()

    def broken(**_):
        raise RuntimeError("裁决器自身故障")

    bus.checker("gate", broken)
    verdict = await bus.check("gate")
    assert not verdict.allowed          # 出错也不放行


async def test_collect_skips_none():
    bus = Bus()
    bus.provider("ctx", lambda **_: "a")
    bus.provider("ctx", lambda **_: None)
    bus.provider("ctx", lambda **_: "b")
    assert await bus.collect("ctx") == ["a", "b"]
