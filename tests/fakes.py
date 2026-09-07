"""Test fixtures: scripted fake model / fake tools, so tests run offline and deterministically."""

from src.kernel import LlmReply, ToolCall, ToolResult


class FakeLlm:
    """Returns LlmReply values from a given script in order; falls back to a default text reply."""

    def __init__(self, scripted=None, default_text="好的"):
        self.scripted = list(scripted or [])
        self.default_text = default_text
        self.calls = []

    async def chat(self, messages, *, tools=None, system=None, on_delta=None):
        self.calls.append(list(messages))
        if self.scripted:
            item = self.scripted.pop(0)
            return item if isinstance(item, LlmReply) else LlmReply(text=str(item))
        return LlmReply(text=self.default_text, tokens=5)


class FakeTools:
    def __init__(self, handlers=None):
        self.handlers = handlers or {}
        self.dispatched = []

    async def dispatch(self, call: ToolCall, ctx=None) -> ToolResult:
        self.dispatched.append(call)
        handler = self.handlers.get(call.name)
        if handler is None:
            return ToolResult.failure(f"unknown tool {call.name}", call.call_id)
        output = handler(call.arguments)
        return ToolResult.success(output, call.call_id)
