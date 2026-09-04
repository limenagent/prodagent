"""body —— 唯一的可组合接口，以及四种内置 body。

内核眼里“能被调度的东西”只有一种：一个满足 NodeBody 协议、
吃 input 和 NodeContext、吐出 Outcome 的执行体。于是：

- 一个纯函数（FnBody）是 body；
- 一次受治理工具调用（ToolBody）是 body；
- 一次固定 prompt 的模型调用（LLMBody）是 body；
- 激活一个子 Agent / 子图（SubPlanBody）也是 body。

没有“宏节点 vs 微 Agent”两套词汇，只有 body 里再套 body——多 Agent
不过是某个 body 递归地又跑起一张图。

Outcome 是 body 的产出，正交地分三块：
- value：给下游节点的值；
- state_delta：要按 reducer 折叠进共享状态的数据；
- control：Goto/Send/Handoff 控制命令（None=沿静态边自然走）；
- suspend：非空则请求在这一点放手暂停（Interrupt）。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from src.kernel.command import Command, Goto, Handoff, Send
from src.kernel.run import Interrupt


@dataclass(frozen=True)
class Outcome:
    value: Any = None
    state_delta: dict[str, Any] = field(default_factory=dict)
    # control 可以是一条命令，也可以是一组命令（一次扇出多个 Send）。
    control: Command | list[Command] | None = None
    suspend: Interrupt | None = None
    # 门面层运行期的“名字→对象”瞬态绑定（如 hand_off 传了 Agent 对象）；
    # 它不进状态、不参与序列化，内核命令本身仍只带名字。
    bindings: dict | None = None

    # —— 便捷构造，读起来像在“陈述意图” ——
    @classmethod
    def ok(cls, value: Any = None, **delta: Any) -> Outcome:
        return cls(value=value, state_delta=dict(delta))

    @classmethod
    def goto(cls, target: str, value: Any = None, **delta: Any) -> Outcome:
        return cls(value=value, state_delta=dict(delta), control=Goto(target))

    @classmethod
    def send(cls, template: str, payload: Any, key: str | None = None) -> Outcome:
        return cls(control=Send(template, payload, key))

    @classmethod
    def fan_out(cls, *sends: Send, **delta: Any) -> Outcome:
        """一次动态扇出到多个实例；**delta 可同时写共享状态（如步骤清单）。"""
        return cls(state_delta=dict(delta), control=list(sends))

    @classmethod
    def handoff(cls, agent: str, task: str) -> Outcome:
        return cls(control=Handoff(agent, task))

    @classmethod
    def park(cls, kind: str, payload: Any = None, question: str = "") -> Outcome:
        return cls(suspend=Interrupt(kind, payload, question))


def coerce_outcome(raw: Any) -> Outcome:
    """让 body 可以“偷懒”：返回裸值/dict/命令，自动规整成 Outcome。"""
    if raw is None:
        return Outcome()
    if isinstance(raw, Outcome):
        return raw
    if isinstance(raw, Command):
        return Outcome(control=raw)
    if isinstance(raw, dict):
        return Outcome(state_delta=raw)
    return Outcome(value=raw)


@runtime_checkable
class NodeBody(Protocol):
    async def run(self, input: Any, ctx: NodeContext) -> Outcome: ...


class NodeContext:
    """body 执行时能用到的“服务接线”。

    注意它是 wiring 不是 data：它持有模型端口、工具端口这些活对象，
    因此不参与序列化；一次运行的数据走 input / state_delta，绝不偷偷
    塞进 context——这条界限让检查点保持干净。
    """

    def __init__(
        self,
        run: Any,
        node_id: str,
        *,
        llm: Any = None,
        tools: Any = None,
        subagent: Any = None,
        bus: Any = None,
        resume_value: Any = None,
    ):
        self.run = run
        self.node_id = node_id
        self._llm = llm
        self._tools = tools
        self._subagent = subagent
        self._bus = bus
        self.resume_value = resume_value

    @property
    def shared(self) -> dict[str, Any]:
        """共享状态只读视图；要改状态请返回 state_delta，由引擎在屏障处折叠。"""
        return self.run.shared

    @property
    def run_id(self) -> str:
        return self.run.run_id

    async def emit(self, event: str, **data: Any) -> None:
        if self._bus is not None:
            await self._bus.fire(event, run_id=self.run_id, node_id=self.node_id, **data)

    async def llm_complete(self, prompt: str, system: str | None = None) -> str:
        """一次固定 prompt 模型调用：处理输入，不决定流程。"""
        reply = await self.llm_chat([{"role": "user", "content": prompt}], system=system)
        return reply.text

    async def llm_chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        system: str | None = None,
        on_delta: Any = None,
    ):
        """走模型端口发起一次对话调用，并统一记账（返回归一 LlmReply）。

        on_delta 透传给实现方做流式“吐字”；记账与归一化仍在这里统一。
        """
        if self._llm is None:
            raise RuntimeError("没有注入 LlmPort，无法执行模型调用")
        reply = await self._llm.chat(messages, tools=tools, system=system, on_delta=on_delta)
        self.run.metrics["llm_calls"] += 1
        self.run.metrics["tokens"] = self.run.metrics.get("tokens", 0) + reply.tokens
        return reply

    async def call_tool(self, name: str, arguments: dict | None = None) -> Any:
        """经工具端口发起一次受治理调用，返回 ToolResult。"""
        if self._tools is None:
            raise RuntimeError("没有注入 ToolPort，无法执行工具")
        from src.kernel.types import ToolCall

        self.run.metrics["tool_calls"] += 1
        # 稳定幂等键：同一节点的同一次尝试重试时不变。
        attempt = self.run.state_of(self.node_id).attempts
        call = ToolCall(name, arguments or {}, call_id=f"{self.run_id}:{self.node_id}:{attempt}")
        return await self._tools.dispatch(call, ctx=self)

    async def spawn(self, spec: Any, task: str, payload: Any = None) -> dict:
        """激活一个子 Run（call 语义：跑完把结果交回来）。"""
        if self._subagent is None:
            raise RuntimeError("没有注入 SubagentPort，无法激活子 Agent")
        return await self._subagent.activate(spec, task, self.run, payload)


# ════════════ 四种内置 body ════════════


class FnBody:
    """L0：一个 Python 函数（普通或 async）。

    函数可以写 f(x) 只拿输入，也可以写 f(x, ctx) 用上服务，内核按形参数量适配。
    """

    def __init__(self, fn: Any):
        self.fn = fn
        self._params = len(inspect.signature(fn).parameters)

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        args = (input, ctx) if self._params >= 2 else (input,)
        result = self.fn(*args)
        if inspect.isawaitable(result):
            result = await result
        return coerce_outcome(result)


class ToolBody:
    """L1：按名字发起一次受治理工具调用，input 即参数。"""

    def __init__(self, tool_name: str):
        self.tool_name = tool_name

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        result = await ctx.call_tool(self.tool_name, input if isinstance(input, dict) else {})
        # 工具成败作为 value 交回，由上游/模型决定下一步，而不是在引擎里抛死。
        return Outcome.ok(result)


class LLMBody:
    """L2：一次固定 prompt 的模型调用。prompt 可以是字符串或 input->str 的函数。"""

    def __init__(self, prompt: Any, system: str | None = None):
        self.prompt = prompt
        self.system = system

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        text = self.prompt if isinstance(self.prompt, str) else self.prompt(input)
        answer = await ctx.llm_complete(text, system=self.system)
        return Outcome.ok(answer)


class SubPlanBody:
    """L3：激活一个子图/子 Agent，用同一套内核递归跑完，fold 其终态。"""

    def __init__(self, spec: Any):
        self.spec = spec

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        result = await ctx.spawn(self.spec, str(input or ""))
        # call 语义：默认只把子 Run 的最终产出交回来，需要完整信息可用自定义 body。
        return Outcome.ok(result.get("output"))
