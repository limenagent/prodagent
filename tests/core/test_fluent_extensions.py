from __future__ import annotations

from prodagent.hooks.checkpoint import BlockingResult, CheckPoint, InjectionPoint
from prodagent.hooks.registry import HookEvent, HookRegistry
from prodagent.llm.fake import script
from prodagent.runtime.agent import Agent


def _agent() -> Agent:
    return Agent(
        name="fluent-ext",
        system_prompt="verify",
        llm=script({"content": "ok"}),
        hooks=HookRegistry(),
    ).reactive()


def _wire(agent: Agent) -> HookRegistry:
    hooks = agent.attach_default_hooks()
    assert hooks is not None
    return hooks


async def test_check_registers_checker_with_enum():
    calls = []

    def veto(**kw):
        calls.append(kw)
        return BlockingResult(blocked=True, reason="nope")

    agent = _agent()
    assert agent.check(CheckPoint.TOOL_CALL, veto) is agent
    hooks = _wire(agent)

    result = await hooks.check_blocking(CheckPoint.TOOL_CALL, name="restart_pod")
    assert result.blocked is True
    assert calls


async def test_check_rejects_non_enum():
    agent = _agent()
    agent.check("tool.call", lambda **kw: None)  # type: ignore[arg-type]
    try:
        _wire(agent)
    except TypeError as exc:
        assert "CheckPoint" in str(exc)
        return
    raise AssertionError("expected TypeError")


async def test_on_accepts_hook_event_enum():
    fired = []
    agent = _agent()
    assert agent.on(HookEvent.SESSION_END, lambda **kw: fired.append(kw)) is agent
    hooks = _wire(agent)
    await hooks.fire(HookEvent.SESSION_END, state="completed")
    assert fired


async def test_on_runs_coroutine_handler():
    done = []

    async def handler(**kw):
        done.append(kw)

    agent = _agent()
    agent.on(HookEvent.SESSION_END, handler)
    hooks = _wire(agent)
    await hooks.fire(HookEvent.SESSION_END, final_output="x")
    assert done


async def test_inject_legacy_single_arg_defaults_to_context_injector():
    agent = _agent()
    assert agent.inject(lambda q: f"ctx:{q}") is agent
    hooks = _wire(agent)
    results = await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="disk full")
    assert results == ["ctx:disk full"]


async def test_inject_explicit_point_enum():
    agent = _agent()
    agent.inject(InjectionPoint.CONTEXT_INJECTOR, lambda q: f"c:{q}")
    hooks = _wire(agent)
    assert await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="q") == ["c:q"]


def test_extend_calls_attach_on_each_bundle():
    class _Bundle:
        def __init__(self):
            self.attached_to = None

        def attach(self, hooks):
            self.attached_to = hooks

    b1, b2 = _Bundle(), _Bundle()
    agent = _agent()
    assert agent.extend(b1, b2) is agent
    hooks = _wire(agent)
    assert b1.attached_to is hooks
    assert b2.attached_to is hooks


def test_extend_rejects_bundle_without_attach():
    agent = _agent()
    agent.extend(object())
    try:
        _wire(agent)
    except TypeError as exc:
        assert "no attach" in str(exc)
        return
    raise AssertionError("expected TypeError")


def test_event_bundles_expose_attach():
    from prodagent.hooks.bundles.observability import SpanObserverHooks
    from prodagent.hooks.observers.console import ConsoleObserverHooks

    assert callable(getattr(ConsoleObserverHooks, "attach", None))
    assert callable(getattr(SpanObserverHooks, "attach", None))


def test_console_observer_handles_injection_failed(capsys):
    from prodagent.hooks.observers.console import ConsoleObserverHooks

    observer = ConsoleObserverHooks()
    observer.on_event(
        event_name="injection.failed",
        point="context.injector",
        injector="memory_recaller",
        error="vector store offline",
    )
    out = capsys.readouterr().out
    assert "INJECT" in out
    assert "FAILED" in out
    assert "context.injector" in out
    assert "vector store offline" in out
