from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prodagent.cognition.context.budget import CompressionLevel
from prodagent.kernel.types import StopReason
from prodagent.llm import stream_text

if TYPE_CHECKING:
    from prodagent.core.config import ContextConfig
    from prodagent.kernel.types import MessageList
    from prodagent.llm import LLMClient

logger = logging.getLogger(__name__)

_SUMMARISE_SYSTEM = (
    "You are a lossless context compressor for an AI agent. "
    "Return ONLY a JSON object - no prose, no markdown fences."
)

_SUMMARISE_SCHEMA = """\
Summarize the agent conversation below. Reply with ONLY this JSON (no other text):
{"focus": "<what the agent is working on now, <=15 words>", "done": ["<completed step>", "..."]}

Conversation turns:
"""

_TOPIC_SUMMARISE_SCHEMA = """\
Compress the conversation below to its essence. Reply with ONLY this JSON (no other text):
{"focus": "<the one thing the agent must accomplish next, <=20 words>", "key_result": "<single most important finding so far, <=25 words>"}

Conversation turns:
"""


class Summariser:
    """Compress a list of messages into a structured summary string via LLM."""

    def __init__(self, llm: LLMClient | None, cfg: ContextConfig) -> None:
        self._llm = llm
        self._cfg = cfg

    async def summarise(
        self, messages: MessageList, *, level: CompressionLevel = CompressionLevel.HISTORY_SUMMARY
    ) -> str:
        if not messages or self._llm is None:
            return ""
        return await self._summarise_with_llm(self._llm, messages, level=level)

    async def _summarise_with_llm(
        self, llm: LLMClient, messages: MessageList, *, level: CompressionLevel
    ) -> str:
        from prodagent.llm import LLMConfig

        cfg = self._cfg
        turns: list[str] = []
        for m in messages:
            role = m.get("role", "?")
            content = str(m.get("content", ""))[: cfg.summary_max_chars_per_turn]
            turns.append(f"{role.upper()}: {content}")
        turns_text = "\n---\n".join(turns)
        schema = (
            _TOPIC_SUMMARISE_SCHEMA
            if level == CompressionLevel.TOPIC_SUMMARY
            else _SUMMARISE_SCHEMA
        )
        prompt = schema + turns_text

        llm_cfg = LLMConfig(
            model=cfg.summary_model,
            max_tokens=cfg.summary_max_tokens,
            temperature=0.0,
            enable_prompt_caching=True,
        )

        try:
            response, raw_text = await stream_text(
                llm,
                [{"role": "user", "content": prompt}],
                system=_SUMMARISE_SYSTEM,
                config=llm_cfg,
            )
        except Exception as exc:
            logger.warning("LLM summarisation failed (%s); escalating to EmergencyStage", exc)
            return ""

        raw = raw_text.strip()
        if not raw:
            if (
                response.stop_reason == StopReason.MAX_TOKENS
                and response.output_tokens >= cfg.summary_max_tokens
            ):
                logger.warning(
                    "Summarisation hit max_tokens=%d with no content - raise summary_max_tokens",
                    cfg.summary_max_tokens,
                )
            else:
                logger.debug(
                    "Summarisation LLM returned empty content "
                    "(stop_reason=%r, input_tokens=%d, output_tokens=%d)",
                    response.stop_reason,
                    response.input_tokens,
                    response.output_tokens,
                )
            return ""
        return raw
