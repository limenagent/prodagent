from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from prodagent.cognition.memory.storage import EPISODIC_DEFAULT_TTL_DAYS, MemoryRecord, MemoryType
from prodagent.llm.base import LLMConfig, stream_text
from prodagent.llm.structured_output import extract_json_object

if TYPE_CHECKING:
    from prodagent.core.state.run import AgentRun
    from prodagent.llm.base import LLMClient

logger = logging.getLogger(__name__)

# Must exceed 1024 for models that burn internal reasoning tokens before JSON.
_CLASSIFIER_MAX_TOKENS = 2_048
_CLASSIFIER_INPUT_MAX_CHARS = 1000
_REASONING_MIN_LEN = 80

__all__ = ["MemoryClassifier", "reasoning_texts"]


class MemoryClassifier:
    """Default LLM-based memory classifier."""

    _SYSTEM_PROMPT = """\
You are a memory extraction assistant for an AI agent system.

Read the input text and extract at most ONE reusable memory from it. A memory
is a distilled, self-contained fact/rule/event — NOT a narration of what the
agent did or a compliance status report. Skip the text entirely (return
{"memory_type":"none"}) when it contains nothing worth remembering long-term.

Memory types:
1. CONSTRAINT — A hard rule or prohibition the agent must always follow
   ("NEVER restart a CrashLoopBackOff pod").  Always injected.  Only emit this
   for genuine durable rules, not for one-off compliance acknowledgements.
2. FACT — A current-state fact ("payment-service ProcessBatch() is a known OOM
   hotspot").  Provide a stable entity_id (e.g. "service:payment-service").
3. PREFERENCE — Soft guidance ("run unfiltered tail_logs in parallel with
   filtered queries").  No TTL.
4. EPISODIC — A concrete historical event with a time anchor ("2026-07-14
   PR #4412 removed the buffer-pool in ProcessBatch() causing OOMKill").
   Set ttl_days (e.g. 7).
5. NONE — The text is narration, status, compliance ack, or trivia.  Nothing
   worth persisting.

The "content" field MUST be a single concise sentence (aim for < 200 chars)
capturing the reusable knowledge — not a quote from the input.  Drop hedges,
drop agent self-commentary, drop table formatting.

Return a JSON object with exactly these fields (no prose, no markdown fences):
{
  "memory_type":   "constraint" | "fact" | "preference" | "episodic" | "none",
  "content":       "<one concise sentence of distilled knowledge>",
  "domain":        "<your domain tag, e.g. payments, k8s, auth, general>",
  "ttl_days":      <integer> | null,
  "entity_id":     "<stable key for FACT, e.g. service:payment-api>" | ""
}"""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        *,
        max_retries: int = 2,
    ) -> None:
        self._llm: LLMClient | None = llm_client
        self._max_retries = max_retries

    async def classify(self, raw: str) -> MemoryRecord | None:
        if self._llm is None:
            from prodagent.llm.factory import create_llm_client

            self._llm = create_llm_client()

        for attempt in range(self._max_retries + 1):
            try:
                response, text = await stream_text(
                    self._llm,
                    messages=[{"role": "user", "content": self._build_prompt(raw)}],
                    system=self._SYSTEM_PROMPT,
                    config=LLMConfig(
                        max_tokens=_CLASSIFIER_MAX_TOKENS,
                        temperature=0.0,
                    ),
                )
                if not text.strip():
                    raise json.JSONDecodeError("LLM returned empty content", "", 0)
                return self._parse(raw, text)

            except json.JSONDecodeError as exc:
                logger.warning("Classifier: bad JSON on attempt %d: %s", attempt + 1, exc)
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    logger.warning(
                        "classification parse failed, defaulting to EPISODIC: %.80s", raw
                    )
                    return MemoryRecord(
                        content=raw,
                        memory_type=MemoryType.EPISODIC,
                        domain="general",
                        ttl_days=EPISODIC_DEFAULT_TTL_DAYS,
                    )

            except Exception as exc:
                logger.warning("Classifier: LLM error on attempt %d: %s", attempt + 1, exc)
                if attempt < self._max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    raise RuntimeError(
                        f"Classifier failed after {self._max_retries + 1} attempts"
                    ) from exc
        return None

    def _build_prompt(self, text: str) -> str:
        truncated = (
            text
            if len(text) < _CLASSIFIER_INPUT_MAX_CHARS
            else text[:_CLASSIFIER_INPUT_MAX_CHARS] + " …[truncated]"
        )
        return f"Classify this text as a context entry:\n\nText: {truncated}"

    def _parse(self, raw: str, response_text: str) -> MemoryRecord | None:
        text = response_text.strip()

        try:
            parsed = json.loads(extract_json_object(text))
        except (json.JSONDecodeError, ValueError) as exc:
            raise json.JSONDecodeError(
                f"No valid JSON found in response (len={len(text)}): {exc}",
                text or "",
                0,
            ) from exc

        mem_type_str = str(parsed.get("memory_type") or "").strip().lower()
        if mem_type_str == "none":
            return None

        mem_type = self._to_memory_type(mem_type_str)
        domain = str(parsed.get("domain") or "general").strip() or "general"
        entity_id = str(parsed.get("entity_id") or "").strip()
        ttl_days_raw = parsed.get("ttl_days")
        ttl_days = int(ttl_days_raw) if ttl_days_raw is not None else None

        content = str(parsed.get("content") or "").strip()
        if not content:
            content = raw

        return MemoryRecord(
            content=content,
            memory_type=mem_type,
            entity_id=entity_id,
            domain=domain,
            ttl_days=ttl_days,
        )

    @staticmethod
    def _to_memory_type(value: str | None) -> MemoryType:
        if not value:
            return MemoryType.EPISODIC
        try:
            return MemoryType(value.lower())
        except ValueError:
            logger.warning("Classifier: unknown memory_type %r, defaulting to EPISODIC", value)
            return MemoryType.EPISODIC


def reasoning_texts(run: AgentRun) -> list[str]:
    return [
        str(m.get("content", ""))
        for m in run.messages
        if m.get("role") == "assistant"
        and isinstance(m.get("content"), str)
        and len(m.get("content", "")) > _REASONING_MIN_LEN
    ]
