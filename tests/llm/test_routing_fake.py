"""RoutingFakeLLM — per-agent scripted queues under one shared LLM client."""

from __future__ import annotations

from prodagent import RoutingFakeLLM
from prodagent.kernel.types import LLMResponse, MessageList, StopReason, ToolCall


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, stop_reason=StopReason.END_TURN)


def _tool_resp(name: str) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[ToolCall(name=name, params={})],
        stop_reason=StopReason.TOOL_USE,
    )


async def test_add_routes_on_agent_header() -> None:
    llm = RoutingFakeLLM()
    llm.add("investigator", [_tool_resp("tail_logs"), _resp("done")])
    llm.add("remediator", [_resp("fixed")])

    first = await llm.complete([], system="# investigator Agent\n\n## Context")
    second = await llm.complete([], system="# investigator Agent")
    other = await llm.complete([], system="# remediator Agent")

    assert first.tool_calls and first.tool_calls[0].name == "tail_logs"
    assert second.content == "done"
    assert other.content == "fixed"
    assert llm.call_count == 3


async def test_add_route_matches_arbitrary_marker() -> None:
    llm = RoutingFakeLLM(routes={"RESPOND WITH JSON ONLY": [_resp('{"steps": []}')]})
    resp = await llm.complete([], system="You are a planner. RESPOND WITH JSON ONLY.")
    assert resp.content == '{"steps": []}'


async def test_unmatched_calls_fall_to_default_then_echo() -> None:
    llm = RoutingFakeLLM(default=[_resp("parent turn")])
    llm.add("peer", [_resp("peer turn")])

    assert (await llm.complete([], system="unrelated system")).content == "parent turn"
    fallback = await llm.complete(
        [{"role": "user", "content": "hello?"}], system="unrelated system"
    )
    assert fallback.content == "[fallback] hello?"


async def test_callable_sources_see_message_history() -> None:
    def _saw(messages: MessageList) -> LLMResponse:
        users = [m["content"] for m in messages if m.get("role") == "user"]
        return _resp("|".join(users))

    llm = RoutingFakeLLM(routes={"hist": [_saw]})
    messages: MessageList = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "ack"},
        {"role": "user", "content": "two"},
    ]
    resp = await llm.complete(messages, system="hist marker")
    assert resp.content == "one|two"


async def test_callables_are_standing_responders() -> None:
    """A callable answers every call — it is not consumed like a scripted turn."""
    counter = {"n": 0}

    def _counting(_messages: MessageList) -> LLMResponse:
        counter["n"] += 1
        return _resp(f"answer #{counter['n']}")

    llm = RoutingFakeLLM(default=[_counting])
    a = await llm.complete([], system="any")
    b = await llm.complete([], system="any")
    c = await llm.complete([], system="any")
    assert (a.content, b.content, c.content) == ("answer #1", "answer #2", "answer #3")


async def test_static_then_callable_mixed_queue() -> None:
    llm = RoutingFakeLLM(default=[_resp("first"), lambda _m: _resp("always-after")])
    a = await llm.complete([], system="x")
    b = await llm.complete([], system="x")
    c = await llm.complete([], system="x")
    assert a.content == "first"
    assert b.content == "always-after"
    assert c.content == "always-after"


async def test_streams_chunks_when_asked() -> None:
    llm = RoutingFakeLLM(default=[_resp("hello routing world")])
    chunks: list[str] = []

    async def _collect(word: str) -> None:
        chunks.append(word)

    await llm.complete(
        [{"role": "user", "content": "x"}],
        system="any",
        on_chunk=_collect,
    )
    assert "".join(chunks) == "hello routing world "


def test_satisfies_llm_client_protocol() -> None:
    from prodagent.ports.llm import LLMClient

    assert isinstance(RoutingFakeLLM(), LLMClient)
