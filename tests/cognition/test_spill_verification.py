from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from prodagent.cognition.context.budget import TokenCounter
from prodagent.cognition.context.manager import ContextManager
from prodagent.cognition.context.spill import ToolResultSpillStore
from prodagent.core.config import ContextConfig
from prodagent.core.state.run import AgentRun
from prodagent.tooling.builtin.read_tool_result import make_read_tool_result


def _make_huge_alert_payload(buried_pool: str, buried_error: str) -> str:
    import json

    alerts = [{"ent": f"r1noise{i}cont", "err": "Latency"} for i in range(866)]
    alerts.append({"ent": buried_pool, "err": buried_error})
    return json.dumps({"alerts": alerts, "total": 867}, indent=2)


@pytest.mark.asyncio
async def test_oversized_result_is_spilled_not_inlined():
    spill_dir = Path(tempfile.mkdtemp())
    cfg = ContextConfig(
        max_tokens=200_000,
        spill_tool_results=True,
        spill_preview_chars=2_000,
    )
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())
    cm = ContextManager(config=cfg, system_prompt="sys", spill_store=store)

    big = _make_huge_alert_payload("r1labelservicecont", "LabelGenerationActionException")
    tc = TokenCounter()
    big_tokens = tc.count(big)
    assert big_tokens > 10_000

    run = AgentRun(run_id="t", task="go")
    run.messages.append({"role": "user", "content": "incident"})
    run.messages.append(
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "mcp__rca__correlate_alerts", "arguments": "{}"},
                }
            ],
        }
    )
    from prodagent.cognition.context.tool_results import reduce_on_append
    from prodagent.core.types import ToolCall

    call = ToolCall(call_id="c1", name="mcp__rca__correlate_alerts", params={})
    msg = reduce_on_append({"result": big}, call, cfg, store, max_result_chars=2_000)
    run.messages.append(msg)

    system, messages = await cm.prepare(run, memory_snippets=None, hooks=None)
    total = tc.count(system) + sum(tc.count_message(m) for m in messages)

    assert total < 10_000, f"spill did not keep context small: {total} tokens"
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs and "<spilled" in tool_msgs[0]["content"]
    assert store.spill_count == 1
    spilled_files = [p for p in spill_dir.iterdir() if p.name.startswith("c1")]
    assert len(spilled_files) == 1


@pytest.mark.asyncio
async def test_read_tool_result_recovers_buried_evidence():
    spill_dir = Path(tempfile.mkdtemp())
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())
    big = _make_huge_alert_payload("r1labelservicecont", "LabelGenerationActionException")
    rec = store.spill(content=big, call_id="c1", tool_name="mcp__rca__correlate_alerts")

    tool = make_read_tool_result(store)
    result = await tool(path=str(rec.path), grep_pattern="labelservice")

    assert result.to_wire()["ok"] is True
    assert "r1labelservicecont" in result.to_wire()["content"]
    assert "1 match" in result.to_wire()["content"] or "match(es)" in result.to_wire()["content"]
    assert "r1noise0cont" not in result.to_wire()["content"]
    assert "r1noise500cont" not in result.to_wire()["content"]

    result_err = await tool(path=str(rec.path), grep_pattern="LabelGeneration")
    assert result_err.to_wire()["ok"] is True
    assert "LabelGenerationActionException" in result_err.to_wire()["content"]


@pytest.mark.asyncio
async def test_subthreshold_result_stays_inline():
    spill_dir = Path(tempfile.mkdtemp())
    cfg = ContextConfig(
        max_tokens=200_000,
        spill_tool_results=True,
    )
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())

    from prodagent.cognition.context.tool_results import reduce_on_append
    from prodagent.core.types import ToolCall

    small = '{"alerts": [], "total": 0}'
    call = ToolCall(call_id="c2", name="search_changes", params={})
    msg = reduce_on_append({"result": small}, call, cfg, store, max_result_chars=8_000)

    assert "<spilled" not in msg["content"]
    assert store.spill_count == 0


@pytest.mark.asyncio
async def test_read_tool_result_rejects_path_escape():
    spill_dir = Path(tempfile.mkdtemp())
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())
    tool = make_read_tool_result(store)

    result = await tool(path="/etc/passwd")

    assert result.error is not None
    assert result.error.code == "invalid_spill_path"


@pytest.mark.asyncio
async def test_mcp_wire_result_spilled_as_multiline_json():
    import json

    spill_dir = Path(tempfile.mkdtemp())
    cfg = ContextConfig(
        max_tokens=200_000,
        spill_tool_results=True,
        spill_preview_chars=2_000,
    )
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())

    from prodagent.cognition.context.tool_results import reduce_on_append
    from prodagent.core.types import ToolCall

    inner = json.dumps(
        {
            "alerts": [
                {"ent": "r1noisecont", "err": "Latency"},
                {"ent": "r1labelservicecont", "err": "TimeoutException"},
            ],
            "total": 2,
        }
    )
    call = ToolCall(call_id="c3", name="mcp__rca__correlate_alerts", params={})
    reduce_on_append({"result": inner}, call, cfg, store, max_result_chars=40)

    assert store.spill_count == 1
    spilled_files = [p for p in spill_dir.iterdir() if p.name.startswith("c3")]
    assert len(spilled_files) == 1
    raw = spilled_files[0].read_text(encoding="utf-8")

    assert raw.lstrip().startswith("{"), f"expected JSON, got: {raw[:80]!r}"
    assert "\n" in raw, "spilled file has no newlines — grep/paging will fail"
    lines = raw.splitlines()
    assert len(lines) > 5, f"expected multi-line JSON, got {len(lines)} line(s)"

    assert "'" not in raw, "single-quote Python repr leaked into spill file"
    assert '"result":' not in raw, "MCP wrapper not unwrapped"

    tool = make_read_tool_result(store)
    result = await tool(path=str(spilled_files[0]), grep_pattern="labelservice")
    assert result.to_wire()["ok"] is True
    assert "r1labelservicecont" in result.to_wire()["content"]
    assert "r1noisecont" not in result.to_wire()["content"], "grep returned the whole file"

    result_err = await tool(path=str(spilled_files[0]), grep_pattern="Timeout")
    assert result_err.to_wire()["ok"] is True
    assert "TimeoutException" in result_err.to_wire()["content"]


@pytest.mark.asyncio
async def test_topology_string_field_is_expanded_to_real_lines():
    import json

    spill_dir = Path(tempfile.mkdtemp())
    cfg = ContextConfig(
        max_tokens=200_000,
        spill_tool_results=True,
    )
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())

    from prodagent.cognition.context.tool_results import reduce_on_append
    from prodagent.core.types import ToolCall

    topology = (
        "**Full Dependency Topology**\n"
        "  r1viexpiocont -> r1audienceservicecont\n"
        "  r1labelbillingcont -> r1chargetcont\n"
        "  r1siosvccont -> r1siosvccont (self-loop)\n"
    )
    inner = json.dumps({"alerts": [{"entity": "r1noisecont"}], "topology": topology, "total": 1})
    call = ToolCall(call_id="c4", name="mcp__rca__correlate_alerts", params={})
    reduce_on_append({"result": inner}, call, cfg, store, max_result_chars=40)

    spilled_file = next(p for p in spill_dir.iterdir() if p.name.startswith("c4"))
    raw = spilled_file.read_text(encoding="utf-8")
    lines = raw.splitlines()

    assert len(lines) > 8, f"topology not expanded: only {len(lines)} lines"

    tool = make_read_tool_result(store)
    result = await tool(path=str(spilled_file), grep_pattern="labelbilling")
    assert result.to_wire()["ok"] is True
    assert "r1labelbillingcont" in result.to_wire()["content"]
    assert "self-loop" not in result.to_wire()["content"], "grep returned the whole topology blob"


@pytest.mark.asyncio
async def test_grep_pattern_supports_regex_alternation():
    spill_dir = Path(tempfile.mkdtemp())
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())
    content = "\n".join(
        [
            "r1labelservicecont",
            "r1labelbillingcont",
            "r1noisecont",
            "r1labelservicecont again",
        ]
    )
    rec = store.spill(content=content, call_id="regex1", tool_name="test")

    tool = make_read_tool_result(store)
    result = await tool(path=str(rec.path), grep_pattern="labelservice|labelbilling")

    assert result.to_wire()["ok"] is True
    assert "3 match" in result.to_wire()["content"]
    assert "r1labelservicecont" in result.to_wire()["content"]
    assert "r1labelbillingcont" in result.to_wire()["content"]
    assert "r1noisecont" not in result.to_wire()["content"]


@pytest.mark.asyncio
async def test_grep_pattern_supports_regex_quantifiers():
    spill_dir = Path(tempfile.mkdtemp())
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())
    content = "\n".join(
        [
            "CHG56777755 deployed at 12:02",
            "CHG56776003 rolled back",
            "no change here",
            "CHG99999999 pending",
        ]
    )
    rec = store.spill(content=content, call_id="regex2", tool_name="test")

    tool = make_read_tool_result(store)
    result = await tool(path=str(rec.path), grep_pattern=r"CHG\d+")

    assert result.to_wire()["ok"] is True
    assert "3 match" in result.to_wire()["content"]
    assert "CHG56777755" in result.to_wire()["content"]
    assert "CHG56776003" in result.to_wire()["content"]
    assert "CHG99999999" in result.to_wire()["content"]
    assert "no change here" not in result.to_wire()["content"]


@pytest.mark.asyncio
async def test_grep_invalid_regex_falls_back_to_substring():
    spill_dir = Path(tempfile.mkdtemp())
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())
    content = "\n".join(["has [unclosed bracket", "no match here", "[unclosed again"])
    rec = store.spill(content=content, call_id="regex3", tool_name="test")

    tool = make_read_tool_result(store)
    result = await tool(path=str(rec.path), grep_pattern="[unclosed")

    assert result.to_wire()["ok"] is True
    assert "2 match" in result.to_wire()["content"]
    assert "has [unclosed bracket" in result.to_wire()["content"]
    assert "[unclosed again" in result.to_wire()["content"]


@pytest.mark.asyncio
async def test_grep_case_insensitive_still_works():
    spill_dir = Path(tempfile.mkdtemp())
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())
    content = "\n".join(["RootCause here", "rootcause there", "ROOTCAUSE everywhere"])
    rec = store.spill(content=content, call_id="regex4", tool_name="test")

    tool = make_read_tool_result(store)
    result = await tool(path=str(rec.path), grep_pattern="rootcause")

    assert result.to_wire()["ok"] is True
    assert "3 match" in result.to_wire()["content"]


def test_safe_name_does_not_collide_on_sanitised_call_ids():
    from prodagent.cognition.context.spill import _safe_name

    a = _safe_name("call/x")
    b = _safe_name("call_x")
    assert a != b, f"call_ids collided on safe_name: {a!r}"


def test_concurrent_spills_get_distinct_files_and_atomic_counter():
    import concurrent.futures
    import tempfile

    spill_dir = Path(tempfile.mkdtemp())
    store = ToolResultSpillStore(spill_dir, counter=TokenCounter())

    def spill_one(i: int) -> str:
        rec = store.spill(content=f"payload-{i}", call_id=f"call_{i}", tool_name="test")
        return str(rec.path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        paths = list(ex.map(spill_one, range(40)))

    assert len(set(paths)) == 40, "concurrent spills collided on filename"
    assert store.spill_count == 40, f"counter undercounted: expected 40, got {store.spill_count}"
