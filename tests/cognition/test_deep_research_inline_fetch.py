"""Regression: deep_research fetch results must spill, not get truncated.

The deep_research demo runs with a small context window (12K) so compression
stages fire visibly. Its mock web pages are ~4000-5000 chars each. ``web_fetch``
declares ``max_result_chars`` below the smallest page so fetch results land on
disk as ``<spilled>`` placeholders; the auto-injected ``read_tool_result`` then
recovers buried evidence via grep. ``read_tool_result`` itself sets
``max_result_chars=inf`` (self-bounding via its ``limit`` param) so its output
is never persisted — persisting would create a Read→file→Read loop.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

from prodagent.cognition.context.budget import TokenCounter
from prodagent.cognition.context.spill import ToolResultSpillStore
from prodagent.cognition.context.tool_results import reduce_on_append
from prodagent.core.config import ContextConfig
from prodagent.core.types import ToolCall


def _deep_research_pages() -> dict[str, str]:
    trader_src = Path(__file__).resolve().parents[2] / "examples" / "deep_research"
    if str(trader_src) not in sys.path:
        sys.path.insert(0, str(trader_src))
    from deep_research.tools import _FAKE_WEB

    return dict(_FAKE_WEB)


def _web_fetch_max_result_chars() -> float:
    trader_src = Path(__file__).resolve().parents[2] / "examples" / "deep_research"
    if str(trader_src) not in sys.path:
        sys.path.insert(0, str(trader_src))
    from deep_research.tools import web_fetch

    return web_fetch.meta.max_result_chars


@pytest.mark.asyncio
async def test_fetch_max_result_chars_below_page_size():
    pages = _deep_research_pages()
    threshold = _web_fetch_max_result_chars()

    research_pages = {u: c for u, c in pages.items() if "injection" not in u}
    smallest_page = min(len(c) for c in research_pages.values())
    assert threshold < smallest_page, (
        f"web_fetch max_result_chars={threshold} is not below the smallest "
        f"research page size ({smallest_page}); fetch results won't spill and "
        f"the LLM has no recovery path after EMERGENCY compression wipes them."
    )


@pytest.mark.asyncio
async def test_read_tool_result_is_self_bounding():
    from prodagent.tooling.builtin.read_tool_result import make_read_tool_result

    store = ToolResultSpillStore(Path("/tmp"), counter=TokenCounter())
    tool = make_read_tool_result(store)
    assert math.isinf(tool.meta.max_result_chars), (
        "read_tool_result must be self-bounding (max_result_chars=inf); "
        "persisting its output creates a Read→file→Read loop."
    )


@pytest.mark.asyncio
async def test_spilled_fetch_is_recoverable_via_read_tool_result():
    from prodagent.tooling.builtin.read_tool_result import make_read_tool_result

    pages = _deep_research_pages()
    cfg = ContextConfig(max_tokens=12_000, spill_preview_chars=800)
    threshold = _web_fetch_max_result_chars()
    sample_url = "https://example.com/gpt4o-bench"
    sample_content = pages[sample_url]

    import tempfile

    store = ToolResultSpillStore(Path(tempfile.mkdtemp()), counter=TokenCounter())
    call = ToolCall(call_id="c_fetch", name="web_fetch", params={"url": sample_url})
    msg = reduce_on_append(
        {"url": sample_url, "content": sample_content, "chars": len(sample_content), "ok": True},
        call,
        cfg,
        store,
        max_result_chars=threshold,
    )

    assert "<spilled" in msg["content"], "fetch-sized result did not spill at demo config"
    assert store.spill_count == 1

    spilled_files = [p for p in store.dir.iterdir() if p.name.startswith("c_fetch")]
    assert len(spilled_files) == 1

    tool = make_read_tool_result(store)
    result = await tool(path=str(spilled_files[0]), grep_pattern="SWE-bench")
    wire = result.to_wire()
    assert wire["ok"] is True
    assert "33%" in wire["content"], "buried benchmark figure not recoverable via grep"
