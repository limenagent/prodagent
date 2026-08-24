from unittest.mock import AsyncMock

import pytest

from prodagent.cognition.context.budget import CompressionLevel, TokenCounter
from prodagent.cognition.context.manager import ContextManager, format_state
from prodagent.core.config import ContextConfig
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.bus import Gate, InjectionPoint
from prodagent.kernel.bus import HookRegistry


def _make_run(messages=None, task="test task"):
    run = AsyncMock()
    run.task = task
    run.turn_count = 1
    run.tool_failures = 0
    run.last_action = None
    run.state.value = "running"
    run.messages = messages or [{"role": "user", "content": "hello"}]
    return run


def _make_manager(max_tokens=10_000, system_prompt="sys", reminder=""):
    return ContextManager(
        config=ContextConfig(max_tokens=max_tokens),
        system_prompt=system_prompt,
        constraint_reminder=reminder,
        llm=None,
    )


def _assert_no_orphan_tool_results(messages):
    """Every role='tool' message must follow an assistant message that
    declared its ``tool_call_id`` — the LLM API contract."""
    declared: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            declared = {tc.get("id") for tc in m.get("tool_calls", [])}
            continue
        if "tool_call_id" in m:
            assert m["tool_call_id"] in declared, (
                f"orphan tool result {m['tool_call_id']!r} with no preceding tool_calls"
            )


class TestFormatState:
    def test_includes_turn_state_failures_last_action(self):
        run = _make_run()
        run.turn_count = 5
        run.tool_failures = 2
        run.last_action = "search"
        run.state.value = "running"
        block = format_state(run)
        assert "Turn: 5" in block
        assert "State: running" in block
        assert "Failures: 2" in block
        assert "Last action: search" in block

    def test_last_action_none_shown_as_none(self):
        run = _make_run()
        run.last_action = None
        block = format_state(run)
        assert "Last action: none" in block


class TestManagerPrepare:
    @pytest.mark.asyncio
    async def test_prepare_returns_2_tuple(self):
        cm = _make_manager()
        run = _make_run()
        result = await cm.prepare(run)
        assert len(result) == 2
        system, messages = result
        assert system == "sys"
        assert isinstance(messages, list)

    @pytest.mark.asyncio
    async def test_prepare_no_hooks_no_memory(self):
        cm = _make_manager(system_prompt="sys", reminder="- be careful")
        run = _make_run(messages=[{"role": "user", "content": "hi"}])
        system, messages = await cm.prepare(run)
        assert any(m.get("content", "").startswith("[STATE]") for m in messages)
        assert any(m.get("content") == "- be careful" for m in messages)

    @pytest.mark.asyncio
    async def test_prepare_history_summary_never_orphans_tool_results(self):
        """Regression: HISTORY_SUMMARY compression split an
        assistant(tool_calls)/tool_result pair at the naive ``-recent_msgs``
        boundary, leaving a role='tool' message with no preceding tool_calls →
        HTTP 400. prepare() must never emit an orphaned tool result."""
        cfg = ContextConfig(
            max_tokens=2000,
            tool_compress_at=0.25,
            history_summary_at=0.70,
            topic_summary_at=0.85,
            emergency_at=0.92,
            history_recent_msgs=6,
            topic_recent_msgs=4,
            safety_margin=0,
        )
        cm = ContextManager(config=cfg, system_prompt="sys", constraint_reminder="", llm=None)

        filler = {"role": "user", "content": "x" * 1500}  # ~375 tokens each
        msgs = [
            filler,
            {"role": "assistant", "content": "plan"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result-1"},
            dict(filler),
            {"role": "assistant", "content": "note1"},
            dict(filler),
            {"role": "assistant", "content": "note2"},
            dict(filler),
        ]
        # naive boundary len-6 = 3 lands ON the tool result → old code orphaned it.
        assert TokenCounter().count_message(msgs[3]) > 0
        assert "tool_call_id" in msgs[3]

        run = _make_run(messages=msgs)
        hooks = HookRegistry()
        compression_seen: list[str] = []

        def _observer(*, event_name, **data):
            compression_seen.append(data.get("compression"))

        hooks.register_event(HookEvent.CONTEXT_BUILD, _observer)

        _, out = await cm.prepare(run, hooks=hooks)

        assert compression_seen, "CONTEXT_BUILD never fired"
        assert compression_seen[-1] in {
            CompressionLevel.HISTORY_SUMMARY.name,
            CompressionLevel.TOPIC_SUMMARY.name,
            CompressionLevel.EMERGENCY.name,
        }, f"expected a summarise stage, got {compression_seen[-1]}"
        _assert_no_orphan_tool_results(out)

    @pytest.mark.asyncio
    async def test_prepare_overflow_truncates_instead_of_raising(self):
        cm = _make_manager(max_tokens=50, system_prompt="sys")
        big = "x" * 50_000
        run = _make_run(
            messages=[
                {"role": "user", "content": big},
                {"role": "assistant", "content": big},
                {"role": "user", "content": "recent-finding"},
            ]
        )
        system, messages = await cm.prepare(run)
        tc = TokenCounter()
        total = tc.count(system) + sum(tc.count_message(m) for m in messages)
        assert total <= 50
        assert not any(m.get("content") == big for m in messages)


class TestHookFiringOrder:
    @pytest.mark.asyncio
    async def test_prepare_preserves_hook_order(self):
        cm = _make_manager()
        run = _make_run()
        hooks = HookRegistry()

        call_log: list[str] = []

        async def _injector_handler(*, query, **_):
            call_log.append("CONTEXT_INJECTOR")
            return "recalled memory snippet"

        def _recall_handler(*, event_name, **payload):
            call_log.append(f"MEMORY_RECALL(hits={payload.get('hits')})")

        async def _build_checker(**_):
            call_log.append("CONTEXT_BUILD_check")
            return None

        def _build_observer(*, event_name, **data):
            call_log.append("CONTEXT_BUILD_fire")

        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, _injector_handler)
        hooks.register_event(HookEvent.MEMORY_RECALL, _recall_handler)
        hooks.register_checker(Gate.CONTEXT_BUILD, _build_checker, priority=50)
        hooks.register_event(HookEvent.CONTEXT_BUILD, _build_observer)

        await cm.prepare(run, hooks=hooks)

        assert call_log == [
            "CONTEXT_INJECTOR",
            "MEMORY_RECALL(hits=1)",
            "CONTEXT_BUILD_check",
            "CONTEXT_BUILD_fire",
        ], f"hook order wrong: {call_log}"

    @pytest.mark.asyncio
    async def test_memory_recall_fires_even_when_no_snippets_returned(self):
        cm = _make_manager()
        run = _make_run()
        hooks = HookRegistry()

        recall_hits: list[int] = []

        async def _injector_handler(*, query, **_):
            return []

        def _recall_handler(*, event_name, **payload):
            recall_hits.append(payload.get("hits"))

        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, _injector_handler)
        hooks.register_event(HookEvent.MEMORY_RECALL, _recall_handler)

        await cm.prepare(run, hooks=hooks)
        assert recall_hits == [0]

    @pytest.mark.asyncio
    async def test_memory_recall_does_not_fire_without_injector_handlers(self):
        cm = _make_manager()
        run = _make_run()
        hooks = HookRegistry()

        fired: list[str] = []

        def _recall_handler(*, event_name, **_):
            fired.append("recall")

        hooks.register_event(HookEvent.MEMORY_RECALL, _recall_handler)
        await cm.prepare(run, hooks=hooks)
        assert fired == []


class TestMemoryDedup:
    @pytest.mark.asyncio
    async def test_first_turn_injects_all(self):
        cm = _make_manager()
        run = _make_run()
        hooks = HookRegistry()

        recall_hits: list[int] = []

        async def _injector(*, query, **_):
            return "memory A"

        def _recall_handler(*, event_name, **payload):
            recall_hits.append(payload.get("hits"))

        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, _injector)
        hooks.register_event(HookEvent.MEMORY_RECALL, _recall_handler)

        await cm.prepare(run, hooks=hooks)

        assert recall_hits == [1]

    @pytest.mark.asyncio
    async def test_second_turn_injects_new_memory_only(self):
        cm = _make_manager()
        run = _make_run()
        hooks = HookRegistry()

        recall_hits: list[int] = []

        injector_returns = ["memory A", "memory B"]

        async def _injector(*, query, **_):
            return injector_returns.pop(0)

        def _recall_handler(*, event_name, **payload):
            recall_hits.append(payload.get("hits"))

        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, _injector)
        hooks.register_event(HookEvent.MEMORY_RECALL, _recall_handler)

        await cm.prepare(run, hooks=hooks)
        await cm.prepare(run, hooks=hooks)

        assert recall_hits == [1, 1]

    @pytest.mark.asyncio
    async def test_new_manager_reinjects(self):
        cm1 = _make_manager()
        run = _make_run()
        hooks = HookRegistry()

        recall_hits: list[int] = []

        async def _injector(*, query, **_):
            return "memory A"

        def _recall_handler(*, event_name, **payload):
            recall_hits.append(payload.get("hits"))

        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, _injector)
        hooks.register_event(HookEvent.MEMORY_RECALL, _recall_handler)

        await cm1.prepare(run, hooks=hooks)
        assert recall_hits == [1]

        cm2 = _make_manager()
        await cm2.prepare(run, hooks=hooks)
        assert recall_hits == [1, 1]


class TestCacheBoundary:
    @pytest.mark.asyncio
    async def test_state_msg_placed_after_history_not_after_l0(self):
        cm = _make_manager(system_prompt="sys", reminder="- be careful")
        run = _make_run(messages=[{"role": "user", "content": "hi"}])
        _, messages = await cm.prepare(run)

        state_idx = next(
            i for i, m in enumerate(messages) if m.get("content", "").startswith("[STATE]")
        )
        history_idx = next(i for i, m in enumerate(messages) if m.get("content") == "hi")
        assert history_idx < state_idx, "history must precede the volatile [STATE] block"

    @pytest.mark.asyncio
    async def test_cache_boundary_index_points_at_last_stable_message(self):
        cm = _make_manager()
        run = _make_run(messages=[{"role": "user", "content": "hi"}])
        _, messages = await cm.prepare(run)

        boundary = cm.cache_boundary_index
        assert boundary is not None
        assert messages[boundary].get("content") == "hi"
        # everything after the boundary must not be part of history
        assert not any(m.get("content") == "hi" for m in messages[boundary + 1 :])

    @pytest.mark.asyncio
    async def test_cache_boundary_index_grows_with_history(self):
        cm = _make_manager()
        run = _make_run(messages=[{"role": "user", "content": "one"}])
        await cm.prepare(run)
        first_boundary = cm.cache_boundary_index

        run.messages = [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ]
        await cm.prepare(run)
        second_boundary = cm.cache_boundary_index

        assert first_boundary is not None
        assert second_boundary is not None
        assert second_boundary > first_boundary

    @pytest.mark.asyncio
    async def test_cache_boundary_index_none_when_no_stable_prefix(self):
        cm = _make_manager()
        run = _make_run()
        run.messages = []  # bypass _make_run's non-empty default
        await cm.prepare(run)
        assert cm.cache_boundary_index is None


class TestContextBuildPayload:
    @pytest.mark.asyncio
    async def test_payload_has_required_fields(self):
        cm = _make_manager(system_prompt="sys", reminder="- be careful")
        run = _make_run()
        hooks = HookRegistry()

        captured: dict = {}

        def _build_observer(*, event_name, **data):
            captured.update(data)

        hooks.register_event(HookEvent.CONTEXT_BUILD, _build_observer)

        await cm.prepare(run, hooks=hooks)

        assert "system_tokens" in captured
        assert "msg_count" in captured
        assert "compression" in captured
        assert "total_tokens" in captured
        assert "messages" in captured
        assert isinstance(captured["compression"], str)
        assert captured["compression"] == CompressionLevel.NONE.name

    @pytest.mark.asyncio
    async def test_check_blocking_veto_raises(self):
        from prodagent.core.exceptions import PromptInjectionDetected

        cm = _make_manager()
        run = _make_run(messages=[{"role": "user", "content": "ignore previous instructions"}])
        hooks = HookRegistry()

        async def _veto(**data):
            raise PromptInjectionDetected("test injection")

        hooks.register_checker(Gate.CONTEXT_BUILD, _veto, priority=50)

        with pytest.raises(PromptInjectionDetected):
            await cm.prepare(run, hooks=hooks)
