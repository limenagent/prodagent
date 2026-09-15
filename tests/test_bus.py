"""Bus's two protocols: observing never affects the main flow, checking is fail-closed."""

from src.kernel import Bus


async def test_fire_observer_error_is_swallowed():
    bus = Bus()

    async def boom(**_):
        raise RuntimeError("观察者炸了")

    seen = []
    bus.on("e", boom)
    bus.on("e", lambda **kw: seen.append(kw))
    await bus.fire("e", x=1)  # must not raise
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
    assert not verdict.allowed  # an error must not let it through
