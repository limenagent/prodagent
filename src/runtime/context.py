"""context —— 上下文窗口管理是一种可替换策略（第 22 课）。

上下文窗口不是内存，它是每次调用模型前，从完整对话历史里“现场装配”出的一个
投影。怎么装、装多少、超了怎么办，全部是策略：

- WindowContext：只保留首轮诉求 + 最近若干条，最简单；
- SummarizingContext：超过预算时，把较早的消息交模型压成一段摘要，再与最近
  消息拼接，既不硬丢早期关键信息，也不撑爆窗口。

这里用“消息条数”做预算，方便教学；生产里把计数换成 tokenizer 的 token 计数
即可，装配流程完全一样。内核不认识这些类，它们只在 ReAct 配方的 think 前被调用。
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol


class ContextManager(Protocol):
    async def assemble(self, messages: list[dict]) -> list[dict]: ...


class WindowContext:
    """保留首轮用户诉求，再保留最近 keep_last 条。"""

    def __init__(self, keep_last: int = 8):
        self.keep_last = keep_last

    async def assemble(self, messages: list[dict]) -> list[dict]:
        if len(messages) <= self.keep_last + 1:
            return messages
        head = next((m for m in messages if m.get("role") == "user"), None)
        tail = messages[-self.keep_last :]
        return ([head] if head else []) + tail


class SummarizingContext:
    """旧消息超预算时压成摘要，拼上最近消息（摘要器走 LlmPort，可被替换）。"""

    def __init__(self, llm: Any, max_messages: int = 10, keep_last: int = 4):
        self.llm = llm
        self.max_messages = max_messages
        self.keep_last = keep_last

    async def assemble(self, messages: list[dict]) -> list[dict]:
        if len(messages) <= self.max_messages:
            return messages
        pivot = len(messages) - self.keep_last
        old, recent = messages[:pivot], messages[pivot:]
        reply = await self.llm.chat(
            [
                {
                    "role": "user",
                    "content": "把下面的对话压缩成要点摘要，保留关键事实、数字和未决事项，不要展开：\n"
                    + _render(old),
                }
            ]
        )
        summary = {"role": "system", "content": f"此前对话摘要：{reply.text}"}
        return [summary, *recent]


def _render(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = (
            m.get("content") or m.get("text") or str({k: v for k, v in m.items() if k != "role"})
        )
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


# —— 五级压缩：按填充率逐级升级，便宜的机械手段先用，只有摘要级才花一次 LLM ——
class CompressionLevel:
    NONE = 0  # 没超窗口：不动
    TOOL_COMPRESS = 1  # 规则化压缩超长工具结果（不调模型）
    HISTORY_SUMMARY = 2  # 摘要较早轮次，保留较多近期原文
    TOPIC_SUMMARY = 3  # 更激进：只留很少近期原文，其余压成主题摘要
    EMERGENCY = 4  # 兜底：只留最近两条 + 最近一条摘要

    NAME: ClassVar[dict[int, str]] = {
        0: "NONE",
        1: "TOOL_COMPRESS",
        2: "HISTORY_SUMMARY",
        3: "TOPIC_SUMMARY",
        4: "EMERGENCY",
    }


def _tool_groups(messages: list[dict]) -> list[list[dict]]:
    """把消息切成“原子组”：一次工具调用的 assistant 与它的 tool 结果必须同组，

    裁剪时整组丢弃，绝不留下没有父调用的孤儿 tool 结果（那会直接触发模型报错）。
    """
    groups: list[list[dict]] = []
    cur: list[dict] | None = None
    for m in messages:
        is_call = m.get("role") == "assistant" and bool(m.get("tool_calls"))
        is_result = m.get("role") == "tool"
        if is_call:  # 工具调用父消息开一组
            if cur:
                groups.append(cur)
            cur = [m]
        elif is_result and cur is not None:  # 结果并入当前组
            cur.append(m)
        else:  # 普通消息关闭当前组
            if cur:
                groups.append(cur)
                cur = None
            groups.append([m])
    if cur:
        groups.append(cur)
    return groups


def _fit_tail(messages: list[dict], capacity: int) -> list[dict]:
    """保留最近、且总条数不超过 capacity 的若干“完整原子组”。"""
    groups = _tool_groups(messages)
    kept: list[list[dict]] = []
    used = 0
    for g in reversed(groups):
        if used + len(g) > capacity and kept:  # 再放就超，且已经有内容
            break
        kept.append(g)
        used += len(g)
    return [m for g in reversed(kept) for m in g]


def _shrink_tool_text(content: str, limit: int = 160) -> str:
    """规则化压缩一条超长工具结果：保留头尾，中间省略（不调模型）。"""
    if not isinstance(content, str) or len(content) <= limit:
        return content
    head, tail = content[: limit * 3 // 5], content[-limit // 5 :]
    return f"{head}\n…[省略 {len(content) - len(head) - len(tail)} 字]…\n{tail}"


class TieredCompactionContext:
    """五级逐级压缩的上下文策略（对应专栏“上下文窗口是现场装配的投影”）。

    用消息条数估算填充率，方便教学；生产把 _size 换成 tokenizer 计数即可，
    逐级升级与“机械优先、摘要才花模型钱”的结构完全不变。
    """

    def __init__(
        self, summarizer: Any, *, capacity: int = 12, history_recent: int = 6, topic_recent: int = 3
    ):
        self.summarizer = summarizer  # 只在摘要级才会被调用
        self.capacity = capacity
        self.history_recent = history_recent
        self.topic_recent = topic_recent
        self.last_level = CompressionLevel.NONE  # 最近一次装配选了哪级，便于观察

    @staticmethod
    def _size(messages: list[dict]) -> int:
        return len(messages)

    def _pick_level(self, ratio: float) -> int:
        if ratio < 1.5:
            return CompressionLevel.TOOL_COMPRESS
        if ratio < 2.5:
            return CompressionLevel.HISTORY_SUMMARY
        if ratio < 4:
            return CompressionLevel.TOPIC_SUMMARY
        return CompressionLevel.EMERGENCY

    async def _summarize(self, older: list[dict], title: str) -> dict:
        reply = await self.summarizer.chat(
            [
                {
                    "role": "user",
                    "content": f"把下面较早的对话压成{title}，保留关键事实、数字与未决事项：\n"
                    + _render(older),
                }
            ]
        )
        return {"role": "system", "content": f"[{title}] {reply.text}"}

    async def assemble(self, messages: list[dict]) -> list[dict]:
        size = self._size(messages)
        if size <= self.capacity:  # 0 级：窗口够用，原样返回
            self.last_level = CompressionLevel.NONE
            return messages

        ratio = size / self.capacity
        level = self._pick_level(ratio)
        self.last_level = level

        if level == CompressionLevel.TOOL_COMPRESS:
            # 1 级：只把超长工具结果就地缩短，一条一条消息都不丢（仍不调模型）。
            shrunk = [
                {**m, "content": _shrink_tool_text(str(m.get("content", "")))}
                if m.get("role") == "tool"
                else m
                for m in messages
            ]
            return _fit_tail(shrunk, self.capacity)

        if level == CompressionLevel.HISTORY_SUMMARY:
            return await self._summary_level(messages, self.history_recent, "历史摘要")
        if level == CompressionLevel.TOPIC_SUMMARY:
            return await self._summary_level(messages, self.topic_recent, "主题摘要")

        # 4 级紧急：只留最近两条原文，外加最近一条已有摘要，再原子裁剪兜底。
        tail = _fit_tail(messages, 2)
        last_summary = next(
            (
                m
                for m in reversed(messages)
                if str(m.get("content", "")).startswith(("[历史摘要]", "[主题摘要]"))
            ),
            None,
        )
        out = ([last_summary] if last_summary else []) + tail
        return _fit_tail(out, self.capacity)

    async def _summary_level(self, messages: list[dict], recent_n: int, title: str):
        # 近期窗口的切点不能落在孤儿 tool 结果上：向前回退到原子组边界。
        idx = max(0, len(messages) - recent_n)
        while 0 < idx < len(messages) and messages[idx].get("role") == "tool":
            idx -= 1
        older, recent = messages[:idx], messages[idx:]
        head = [await self._summarize(older, title)] if older else []
        return _fit_tail(head + recent, self.capacity)
