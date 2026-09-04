from __future__ import annotations

from prodagent import Agent, AgentConfig
from prodagent.hooks.bundles.memory import MemoryHooks
from prodagent.kernel.bus import HookEvent, HookRegistry, InjectionPoint
from prodagent.llm.fake import script


class _Store:
    def __init__(self) -> None:
        self.classified = False

    async def recall(self, query: str, domain: str | None = None) -> str:
        return f"recalled:{query}"

    async def classify(self, **kw) -> None:
        self.classified = True


def _agent() -> Agent:
    return Agent(
        "mem-bundle",
        system_prompt="verify",
        config=AgentConfig(
            name="mem-bundle",
            llm=script({"content": "ok"}),
            hooks=HookRegistry(),
        ),
    )


async def test_memory_hooks_registers_injector_and_classify_event():
    manager = _Store()
    hooks = HookRegistry()
    MemoryHooks(manager).attach(hooks)

    results = await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="disk full")
    assert results == ["recalled:disk full"]

    await hooks.fire(HookEvent.SESSION_END)
    assert manager.classified is True


async def test_memory_hooks_auto_attaches_with_registry_kwarg():
    manager = _Store()
    hooks = HookRegistry()
    MemoryHooks(manager).attach(hooks)
    assert await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="q") == ["recalled:q"]


async def test_memory_hooks_wires_only_defined_methods():
    class _RecallOnly:
        async def recall(self, query: str, domain: str | None = None) -> str:
            return "hit" if query else None

    hooks = HookRegistry()
    MemoryHooks(_RecallOnly()).attach(hooks)
    assert await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="q") == ["hit"]
    await hooks.fire(HookEvent.SESSION_END)


async def test_memory_hooks_plugs_in_via_extend():
    manager = _Store()
    agent = Agent(
        "mem-bundle",
        system_prompt="verify",
        config=AgentConfig(
            name="mem-bundle",
            llm=script({"content": "ok"}),
            hooks=HookRegistry(),
            extensions=[MemoryHooks(manager)],
        ),
    )

    hooks = agent.attach_default_hooks()
    assert await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="x") == ["recalled:x"]


def test_memory_verb_no_longer_exists():
    agent = _agent()
    assert not hasattr(agent, "memory")


def test_bundles_do_not_accept_hook_registry_kwarg():
    import inspect

    from prodagent.hooks.bundles.observability import SpanObserverHooks
    from prodagent.hooks.bundles.security import ApprovalHooks
    from prodagent.hooks.observers.console import ConsoleObserverHooks

    for cls in [
        MemoryHooks,
        ConsoleObserverHooks,
        SpanObserverHooks,
        ApprovalHooks,
    ]:
        sig = inspect.signature(cls.__init__)
        params = sig.parameters
        bad = [
            name
            for name in params
            if name != "self" and name in ("hooks", "hook_registry", "registry")
        ]
        assert not bad, (
            f"{cls.__name__} accepts {bad} — bundles must wire via .attach(hooks), "
            "not a constructor hooks= kwarg"
        )
        assert callable(getattr(cls, "attach", None)), (
            f"{cls.__name__} has no attach(hooks) method — required by Bundle protocol"
        )
