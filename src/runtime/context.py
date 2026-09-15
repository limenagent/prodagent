"""context — context-window management as a replaceable strategy.

The context window is not memory; before every model call it is a projection
"assembled on the spot" from the full conversation history. How to assemble it,
how much to keep, and what to do when it overflows are all strategy. The
teaching build ships one strategy, five-level escalation: within the window
leave it untouched; past capacity, shorten tool results mechanically first (no
model spend), then summarize level by level — only the summary levels spend a
model call.

Here "message count" is used as the budget for teaching; in production swap the
counter for a tokenizer's token count and the assembly flow is identical. The
kernel doesn't know this class; it's only invoked before "think" in the ReAct
recipe.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol


class ContextManager(Protocol):
    async def assemble(self, messages: list[dict]) -> list[dict]: ...


def _render(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = (
            m.get("content") or m.get("text") or str({k: v for k, v in m.items() if k != "role"})
        )
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


# ---- Five-level compression: escalate by fill ratio; use cheap mechanical means
# first, and spend one LLM call only at the summary levels. ----
class CompressionLevel:
    NONE = 0  # within window: leave untouched
    TOOL_COMPRESS = 1  # rule-based shrink of over-long tool results (no model)
    HISTORY_SUMMARY = 2  # summarize earlier rounds, keep more recent verbatim
    TOPIC_SUMMARY = (
        3  # more aggressive: keep very little recent verbatim, summarize the rest by topic
    )
    EMERGENCY = 4  # fallback: keep only the latest two messages + the latest prior summary

    NAME: ClassVar[dict[int, str]] = {
        0: "NONE",
        1: "TOOL_COMPRESS",
        2: "HISTORY_SUMMARY",
        3: "TOPIC_SUMMARY",
        4: "EMERGENCY",
    }


def _tool_groups(messages: list[dict]) -> list[list[dict]]:
    """Split messages into "atomic groups": the assistant message of one tool call must stay grouped with its tool results.

    When trimming, drop whole groups, never leaving an orphan tool result with no
    parent call (that would immediately make the model error out).
    """
    groups: list[list[dict]] = []
    cur: list[dict] | None = None
    for m in messages:
        is_call = m.get("role") == "assistant" and bool(m.get("tool_calls"))
        is_result = m.get("role") == "tool"
        if is_call:  # a tool-call parent message opens a group
            if cur:
                groups.append(cur)
            cur = [m]
        elif is_result and cur is not None:  # results join the current group
            cur.append(m)
        else:  # an ordinary message closes the current group
            if cur:
                groups.append(cur)
                cur = None
            groups.append([m])
    if cur:
        groups.append(cur)
    return groups


def _fit_tail(messages: list[dict], capacity: int) -> list[dict]:
    """Keep the most recent complete atomic groups whose total count stays within capacity."""
    groups = _tool_groups(messages)
    kept: list[list[dict]] = []
    used = 0
    for g in reversed(groups):
        if (
            used + len(g) > capacity and kept
        ):  # adding it would overflow and we already have something
            break
        kept.append(g)
        used += len(g)
    return [m for g in reversed(kept) for m in g]


def _shrink_tool_text(content: str, limit: int = 160) -> str:
    """Rule-based shrink of one over-long tool result: keep head and tail, elide the middle (no model)."""
    if not isinstance(content, str) or len(content) <= limit:
        return content
    head, tail = content[: limit * 3 // 5], content[-limit // 5 :]
    return f"{head}\n...[{len(content) - len(head) - len(tail)} chars omitted]...\n{tail}"


class TieredCompactionContext:
    """A five-level, gradually escalating context strategy (matches "the context window is an assembled projection").

    Fill ratio is estimated by message count for teaching; in production swap
    _size for a tokenizer count, and the gradual escalation plus "mechanical
    first, spend model money only to summarize" structure stays unchanged.
    """

    def __init__(
        self, summarizer: Any, *, capacity: int = 12, history_recent: int = 6, topic_recent: int = 3
    ):
        self.summarizer = summarizer  # called only at summary levels
        self.capacity = capacity
        self.history_recent = history_recent
        self.topic_recent = topic_recent
        self.last_level = (
            CompressionLevel.NONE
        )  # which level the latest assembly chose, for observability

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
                    "content": f"Compress the earlier conversation below into a {title}, "
                    "keeping key facts, numbers, and open items:\n" + _render(older),
                }
            ]
        )
        return {"role": "system", "content": f"[{title}] {reply.text}"}

    async def assemble(self, messages: list[dict]) -> list[dict]:
        size = self._size(messages)
        if size <= self.capacity:  # level 0: window is sufficient, return as-is
            self.last_level = CompressionLevel.NONE
            return messages

        ratio = size / self.capacity
        level = self._pick_level(ratio)
        self.last_level = level

        if level == CompressionLevel.TOOL_COMPRESS:
            # Level 1: only shorten over-long tool results in place, dropping no
            # message at all (still no model call).
            shrunk = [
                {**m, "content": _shrink_tool_text(str(m.get("content", "")))}
                if m.get("role") == "tool"
                else m
                for m in messages
            ]
            return _fit_tail(shrunk, self.capacity)

        if level == CompressionLevel.HISTORY_SUMMARY:
            return await self._summary_level(messages, self.history_recent, "History summary")
        if level == CompressionLevel.TOPIC_SUMMARY:
            return await self._summary_level(messages, self.topic_recent, "Topic summary")

        # Level 4 emergency: keep only the latest two verbatim messages plus the
        # latest existing summary, then fall back to atomic trimming.
        tail = _fit_tail(messages, 2)
        last_summary = next(
            (
                m
                for m in reversed(messages)
                if str(m.get("content", "")).startswith(("[History summary]", "[Topic summary]"))
            ),
            None,
        )
        out = ([last_summary] if last_summary else []) + tail
        return _fit_tail(out, self.capacity)

    async def _summary_level(self, messages: list[dict], recent_n: int, title: str):
        # The recent-window cut must not land on an orphan tool result: walk back
        # to an atomic-group boundary.
        idx = max(0, len(messages) - recent_n)
        while 0 < idx < len(messages) and messages[idx].get("role") == "tool":
            idx -= 1
        older, recent = messages[:idx], messages[idx:]
        head = [await self._summarize(older, title)] if older else []
        return _fit_tail(head + recent, self.capacity)
