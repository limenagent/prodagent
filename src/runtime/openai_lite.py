"""openai_lite — talk to an OpenAI-compatible /chat/completions using only the stdlib.

The src kernel has zero third-party dependencies, and we don't pull in
openai/httpx here either: one POST via urllib is enough. Any OpenAI-protocol-
compatible service (the official API, gateways, a local vLLM, etc.) works.

Configuration (environment variables):
  OPENAI_API_KEY     API key
  OPENAI_BASE_URL    service URL, default https://api.openai.com/v1
  OPENAI_MODEL       model name, default gpt-4o-mini
  OPENAI_MAX_TOKENS  output budget (including reasoning), default 8192

With on_delta it uses SSE streaming, calling back text fragments as they arrive
(the UI "typewriter" effect), then assembles an LlmReply identical to the
non-streaming path once done.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request

from src.kernel import LlmReply, ToolCall


def _wire(messages: list) -> list[dict]:
    """Internal messages -> OpenAI wire format.

    Runtime messages store kernel ToolCall objects (plain json would fail); here
    we assemble the tool_calls array and match later tool messages by tool_call_id.
    """
    out, ids = [], {}  # name -> call_id assigned in the previous round
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
                    "content": m.get("text")
                    or "",  # strict gateways reject null; "" works everywhere
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
    """Implements the kernel LlmPort: chat(messages, tools, system, on_delta) -> LlmReply."""

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
        # Reasoning models (GLM/DeepSeek, etc.) count their thinking against the
        # output budget; many gateways default to just 1024, so a long reasoning
        # trace truncates the answer to empty. Give enough by default; an env var overrides.
        self.max_tokens = max_tokens or int(os.getenv("OPENAI_MAX_TOKENS", "8192"))

    # ---- request construction and sending (shared by both paths) ----
    def _payload(self, messages, tools, system) -> dict:
        wire = _wire(messages)
        # Context strategies may inject summary messages with role=system
        # mid-window. Strict gateways (GLM et al.) reject a payload carrying
        # more than one system message (400 "messages invalid"), so fold every
        # window system message into the single leading one — hoisted out of
        # its original position, relative order among the summaries kept.
        summaries = [m["content"] for m in wire if m.get("role") == "system"]
        if summaries:  # rare; the common call scans once and rebuilds nothing
            system = "\n\n".join([s for s in (system, *summaries) if s])
            wire = [m for m in wire if m.get("role") != "system"]
        msgs = ([{"role": "system", "content": system}] if system else []) + wire
        payload = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:  # the registry already hands us OpenAI format
            payload["tools"] = tools
        return payload

    def _open(self, payload: dict):
        """Send the request and return the response object; read the body on server errors for debugging."""
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
            raise RuntimeError(f"model API returned {exc.code}: {detail[:300]}") from None

    async def chat(self, messages, *, tools=None, system=None, on_delta=None) -> LlmReply:
        payload = self._payload(messages, tools, system)
        if on_delta is None:
            # No typewriter needed: offload the blocking request to a thread, get it all at once.
            return await asyncio.to_thread(self._chat_sync, payload)
        payload["stream"] = True
        return await self._chat_stream(payload, on_delta)

    def _chat_sync(self, payload: dict) -> LlmReply:
        with self._open(payload) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        return self._reply_from(raw, {})

    async def _chat_stream(self, payload: dict, on_delta) -> LlmReply:
        # Both connecting and reading each line block; offload them to threads,
        # returning to this loop after each line to call on_delta.
        resp = await asyncio.to_thread(self._open, payload)
        text, reasoning, calls, usage, saw_sse = "", "", {}, {}, False
        raws: list[bytes] = []  # diagnostics: keep a few raw SSE lines
        try:
            while True:
                line = await asyncio.to_thread(resp.readline)
                if not line:
                    break
                line = line.strip()
                if not line.startswith(b"data:"):
                    if line.startswith(b"{"):
                        # The gateway didn't answer with SSE (ignored stream or
                        # returned JSON directly): parse the whole thing as a normal response.
                        rest = await asyncio.to_thread(resp.read)
                        reply = self._reply_from(json.loads((line + rest).decode("utf-8")), {})
                        if reply.text:
                            await on_delta(reply.text)
                        return reply
                    continue  # blank line / SSE comment line
                saw_sse = True
                piece = line[5:].strip()
                if piece == b"[DONE]":
                    break
                if len(raws) < 3:
                    raws.append(piece)
                chunk = json.loads(piece)
                usage = chunk.get("usage") or usage
                if not chunk.get("choices"):
                    if chunk.get(
                        "error"
                    ):  # 200 with an embedded error: don't paper over it as "no content"
                        raise RuntimeError(f"model API stream error: {str(chunk['error'])[:300]}")
                    continue
                delta = chunk["choices"][0].get("delta") or {}
                if delta.get(
                    "reasoning_content"
                ):  # reasoning channel of reasoning models (GLM/DeepSeek, etc.)
                    reasoning += delta["reasoning_content"]
                    await on_delta(delta["reasoning_content"], "reasoning")
                if delta.get("content"):
                    text += delta["content"]
                    await on_delta(delta["content"])
                for tc in delta.get("tool_calls") or []:  # arrives in fragments; assemble by index
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    slot["id"] = slot["id"] or tc.get("id", "")
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
        finally:
            resp.close()
        if not text and not calls:
            if reasoning:
                # Some reasoning models (GLM occasionally) put the whole answer in
                # the reasoning channel and leave content empty: adopt reasoning as
                # content rather than handing the caller an empty answer.
                text = reasoning
            elif saw_sse:
                # Genuinely nothing: fail loudly with the scene, never return an empty answer silently.
                sample = b" | ".join(raws)[:300]
                raise RuntimeError(f"model stream returned no content; raw fragments: {sample!r}")
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
        for i in sorted(calls):  # streaming path: add the assembled results
            slot = calls[i]
            try:
                args = json.loads(slot["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(slot["name"], args, slot["id"]))
        tokens = int((raw.get("usage") or {}).get("total_tokens", 0))
        return LlmReply(text=msg.get("content") or "", tool_calls=tool_calls, tokens=tokens)
