"""Tests for the dating-chat example: his hand-rolled truncation loses the
allergy line, her memory + tiered compaction keep it."""

import importlib

from src.runtime.context import CompressionLevel

mod = importlib.import_module("examples.dating_chat")

_CACHE: dict = {}


async def _once(lang: str = "zh"):
    """Run the date once per language and cache everything (scripts are
    consumed on first run)."""
    if lang not in _CACHE:
        wf, a = await mod.build_date(lang)
        result = await wf.run(a["t"]["topic"])
        _CACHE[lang] = (result, a)
    return _CACHE[lang]


def _texts(window):
    return " ".join(str(m.get("content") or "") for m in window)


async def test_niu_window_evicts_allergy_line():
    _, a = await _once()
    anchor = a["t"]["allergy_anchor"]
    seen = a["niu_llm"].messages_seen
    assert len(seen) == 5  # greet, ask, search-call, forward, oblivious
    assert anchor in _texts(seen[1])  # he did see her round-0 line
    assert anchor not in _texts(seen[-1])  # del messages[:-4] evicted it


async def test_mei_escalates_to_history_summary():
    _, a = await _once()
    assert a["mei_context"].last_level == CompressionLevel.HISTORY_SUMMARY
    assert a["mei_context"].summarizer.calls == 1  # one compressor call total


async def test_mei_tool_result_head_and_tail_survive():
    _, a = await _once()
    t = a["t"]
    window = a["mei_llm"].messages_seen[3]  # the TOOL_COMPRESS projection
    tools = [m for m in window if m.get("role") == "tool"]
    assert len(tools) == 1
    content = tools[0]["content"]
    assert "chars omitted" in content  # middle elided mechanically
    assert t["head_sentinel"] in content  # the seafood evidence survives
    assert t["tail_sentinel"] in content  # so does the noise_level tail


async def test_mei_final_window_keeps_allergy_via_summary():
    _, a = await _once()
    window = a["mei_llm"].messages_seen[-1]  # the HISTORY_SUMMARY projection
    assert window[0]["role"] == "system"
    assert window[0]["content"].startswith("[History summary]")
    assert a["t"]["allergy_anchor"] in window[0]["content"]


async def test_date_ends_within_round_cap():
    result, _ = await _once()
    assert result.state["round"] == mod.MAX_ROUNDS - 1
    assert len(result.state["floor"]) == 8  # 4 rounds x 2 speakers
    assert result.metrics["waves"] == 9  # 4+4 turns + the final node
    assert result.status == "completed"


async def test_en_script_smoke():
    result, a = await _once("en")
    assert a["mei_context"].last_level == CompressionLevel.HISTORY_SUMMARY
    assert a["t"]["allergy_anchor"] not in _texts(a["niu_llm"].messages_seen[-1])
    assert result.status == "completed"
