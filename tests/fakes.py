"""测试夹具：脚本化的假模型 / 假工具，让用例离线、确定性地运行。"""

from src.kernel import LlmReply, ToolCall, ToolResult


class FakeLlm:
    """按给定脚本依次返回 LlmReply；不传脚本时默认返回一段文本。"""

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
