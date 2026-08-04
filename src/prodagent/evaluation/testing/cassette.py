"""VCR cassette record/replay for LLM calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prodagent.core.types import LLMResponse

if TYPE_CHECKING:
    from prodagent.core.types import MessageList
    from prodagent.llm.base import ChunkCallback, LLMClient, LLMConfig


class RecordingLLMClient:
    """Wrap a real LLMClient and tee every request/response pair to a JSONL cassette.

    Use as a context manager so the cassette is flushed even when the test body
    raises::

        async with RecordingLLMClient(real, path) as rec:
            ...
    """

    def __init__(self, inner: LLMClient, cassette: Path) -> None:
        self._inner = inner
        self._cassette = Path(cassette)
        self._entries: list[dict[str, Any]] = []
        self._closed = False

    async def __aenter__(self) -> RecordingLLMClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        self.close()

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: ChunkCallback,
    ) -> LLMResponse:
        response = await self._inner.complete(
            messages,
            system=system,
            tools=tools,
            config=config,
            on_chunk=on_chunk,
        )
        self._entries.append(
            {
                "request": {"messages": list(messages), "system": system},
                "response": response.to_dict(),
            }
        )
        return response

    def close(self) -> None:
        """Flush all recorded interactions to the cassette file (one JSON per line)."""
        if self._closed:
            return
        self._closed = True
        self._cassette.parent.mkdir(parents=True, exist_ok=True)
        with self._cassette.open("w", encoding="utf-8") as fh:
            for entry in self._entries:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


class ReplayLLMClient:
    """Replay recorded LLM responses from a cassette — no real client, no network."""

    def __init__(self, cassette: Path) -> None:
        self._cassette = Path(cassette)
        self._responses: list[LLMResponse] = []
        self._cursor = 0
        with self._cassette.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    self._responses.append(LLMResponse.from_dict(json.loads(line)["response"]))

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: ChunkCallback,
    ) -> LLMResponse:
        if self._cursor >= len(self._responses):
            raise IndexError(
                f"Cassette {self._cassette.name!r} exhausted after "
                f"{len(self._responses)} responses — the loop requested more turns "
                "than were recorded. Re-record the cassette."
            )
        response = self._responses[self._cursor]
        self._cursor += 1
        if response.content:
            await on_chunk(response.content)
        return response
