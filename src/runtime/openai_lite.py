"""openai_lite —— 只用标准库对接 OpenAI 兼容的 /chat/completions。

src 内核零三方依赖，这里也不引入 openai/httpx：用 urllib 发一个 POST 就够。
任何兼容 OpenAI 协议的服务（官方、各类网关、本地 vLLM 等）都能用。

配置（环境变量）：
  OPENAI_API_KEY    密钥
  OPENAI_BASE_URL   服务地址，默认 https://api.openai.com/v1
  OPENAI_MODEL      模型名，默认 gpt-4o-mini
  OPENAI_MAX_TOKENS 输出预算（含思考），默认 8192

传了 on_delta 就走 SSE 流式：边收边把文本片段回调出去（UI 上的“吐字”），
收完拼出与普通路径一致的 LlmReply。
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

from src.kernel import LlmReply, ToolCall


def _wire(messages: list) -> list[dict]:
    """内部消息 -> OpenAI 线上格式。

    运行时消息里存的是内核 ToolCall 对象（直接 json 会炸），
    这里拼成 tool_calls 数组，并让后续 tool 消息用 tool_call_id 对上号。
    """
    out, ids = [], {}  # name -> 上一轮分配的 call_id
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for j, tc in enumerate(m["tool_calls"]):
                cid = getattr(tc, "call_id", "") or f"call_{i}_{j}"
                ids[tc.name] = cid
                calls.append(
                    {
                        "id": cid,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                )
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("text") or "",  # 严格网关拒绝 null，空串通吃
                    "tool_calls": calls,
                }
            )
        elif role == "tool":
            name = m.get("name", "")
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": ids.pop(name, f"call_{name}"),
                    "content": str(m.get("content", "")),
                }
            )
        elif role == "assistant":
            out.append({"role": "assistant", "content": m.get("text", "") or ""})
        else:
            out.append({"role": role or "user", "content": str(m.get("content", ""))})
    return out


class OpenAICompatibleLlm:
    """实现内核 LlmPort：chat(messages, tools, system, on_delta) -> LlmReply。"""

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout: float = 60.0,
        max_tokens: int | None = None,
    ):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self.temperature = temperature
        self.timeout = timeout
        # 推理模型（GLM/DeepSeek 等）的思考也计入输出预算；不少网关默认只给 1024，
        # 思考一长正文就被截成空。这里默认给足，可用环境变量覆盖。
        self.max_tokens = max_tokens or int(os.getenv("OPENAI_MAX_TOKENS", "8192"))

    # —— 请求构造与发送（两条路径共用）——
    def _payload(self, messages, tools, system) -> dict:
        msgs = ([{"role": "system", "content": system}] if system else []) + _wire(messages)
        payload = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:  # 注册表给的就是 OpenAI 格式
            payload["tools"] = tools
        return payload

    def _open(self, payload: dict):
        """发请求、返回响应对象；服务端错误读出正文，便于排查。"""
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")
            raise RuntimeError(f"模型接口返回 {exc.code}：{detail[:300]}") from None

    async def chat(self, messages, *, tools=None, system=None, on_delta=None) -> LlmReply:
        payload = self._payload(messages, tools, system)
        if on_delta is None:
            # 不需要吐字：阻塞请求丢到线程里，一次拿全。
            return await asyncio.to_thread(self._chat_sync, payload)
        payload["stream"] = True
        return await self._chat_stream(payload, on_delta)

    def _chat_sync(self, payload: dict) -> LlmReply:
        with self._open(payload) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        return self._reply_from(raw, {})

    async def _chat_stream(self, payload: dict, on_delta) -> LlmReply:
        # 连接与每次读行都是阻塞的，各丢进线程，读完一行回到循环上回调 on_delta。
        resp = await asyncio.to_thread(self._open, payload)
        text, reasoning, calls, usage, saw_sse = "", "", {}, {}, False
        raws: list[bytes] = []  # 诊断用：留几行原始 SSE
        try:
            while True:
                line = await asyncio.to_thread(resp.readline)
                if not line:
                    break
                line = line.strip()
                if not line.startswith(b"data:"):
                    if line.startswith(b"{"):
                        # 网关没按 SSE 回（忽略 stream 或直接回了 JSON）：整段按普通响应解析。
                        rest = await asyncio.to_thread(resp.read)
                        reply = self._reply_from(json.loads((line + rest).decode("utf-8")), {})
                        if reply.text:
                            await on_delta(reply.text)
                        return reply
                    continue  # 空行/SSE 注释行
                saw_sse = True
                piece = line[5:].strip()
                if piece == b"[DONE]":
                    break
                if len(raws) < 3:
                    raws.append(piece)
                chunk = json.loads(piece)
                usage = chunk.get("usage") or usage
                if not chunk.get("choices"):
                    if chunk.get("error"):  # 200 但内嵌错误：别当“无内容”糊弄过去
                        raise RuntimeError(f"模型接口流式返回错误：{str(chunk['error'])[:300]}")
                    continue
                delta = chunk["choices"][0].get("delta") or {}
                if delta.get("reasoning_content"):  # 推理模型的思考通道（GLM/DeepSeek 等）
                    reasoning += delta["reasoning_content"]
                    await on_delta(delta["reasoning_content"], "reasoning")
                if delta.get("content"):
                    text += delta["content"]
                    await on_delta(delta["content"])
                for tc in delta.get("tool_calls") or []:  # 分片到达，按 index 拼装
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = slot["id"] or tc.get("id", "")
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
        finally:
            resp.close()
        if not text and not calls:
            if reasoning:
                # 有的推理模型（GLM 偶发）把完整答案都放在思考通道、正文留空：
                # 采纳思考为正文，别让调用方拿到空回答。
                text = reasoning
            elif saw_sse:
                # 真的什么都没有：大声失败并带回现场，别静默给出空回答。
                sample = b" | ".join(raws)[:300]
                raise RuntimeError(f"模型流式响应无正文，原始片段：{sample!r}")
        return self._reply_from(
            {"choices": [{"message": {"content": text}}], "usage": usage}, calls
        )

    @staticmethod
    def _reply_from(raw: dict, calls: dict[int, dict]) -> LlmReply:
        msg = raw["choices"][0]["message"]
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(fn.get("name", ""), args, tc.get("id", "")))
        for i in sorted(calls):  # 流式路径：用拼装结果补进来
            slot = calls[i]
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(slot["name"], args, slot["id"]))
        tokens = int((raw.get("usage") or {}).get("total_tokens", 0))
        return LlmReply(text=msg.get("content") or "", tool_calls=tool_calls, tokens=tokens)
