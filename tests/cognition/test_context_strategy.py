import json
from unittest.mock import AsyncMock

import pytest

from prodagent.cognition.context.budget import CompressionLevel, TokenCounter
from prodagent.cognition.context.compression import (
    EmergencyStage,
    HistoryCompressor,
    NoCompressionStage,
    StageContext,
    Summariser,
    SummarizeStage,
    ToolCompressStage,
    fit_budget,
    safe_tail_start,
)
from prodagent.core.config import ContextConfig


@pytest.fixture
def cfg():
    return ContextConfig(max_tokens=100_000)


@pytest.fixture
def counter():
    return TokenCounter()


@pytest.fixture
def ctx(cfg, counter):
    return StageContext(
        run=None,
        counter=counter,
        config=cfg,
        summariser=Summariser(None, cfg),
    )


def _assert_no_orphan_tool_results(messages):
    """Every role='tool' message must follow an assistant message that
    declared its ``tool_call_id`` — the LLM API contract."""
    declared: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            declared = {tc.get("id") for tc in m.get("tool_calls", [])}
            continue
        if "tool_call_id" in m:
            assert m["tool_call_id"] in declared, (
                f"orphan tool result {m['tool_call_id']!r} with no preceding tool_calls"
            )


class TestFitBudget:
    def test_negative_budget_returns_empty_silently(self, counter):
        msgs = [{"role": "user", "content": "hello"}]
        result = fit_budget(msgs, -100, counter)
        assert result == []

    def test_fits_within_budget(self, counter):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
        result = fit_budget(msgs, 10_000, counter)
        assert result == msgs

    def test_drops_oldest_when_over_budget(self, counter):
        msgs = [
            {"role": "user", "content": "a" * 1000},
            {"role": "assistant", "content": "b" * 1000},
            {"role": "user", "content": "c" * 1000},
        ]
        result = fit_budget(msgs, counter.count("c" * 1000), counter)
        assert len(result) == 1
        assert result[0]["content"] == "c" * 1000

    def test_preserves_order(self, counter):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ]
        result = fit_budget(msgs, 10_000, counter)
        assert [m["content"] for m in result] == ["first", "second"]


class TestFitBudgetToolPairAtomic:
    def test_does_not_split_assistant_call_from_its_result(self, counter):
        big = "x" * 500
        msgs = [
            {"role": "user", "content": "please call foo"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": big},
            {"role": "assistant", "content": "done"},
        ]
        budget = counter.count(big) + counter.count_message(
            {"role": "assistant", "content": "done"}
        )
        result = fit_budget(msgs, budget, counter)
        has_result = any(m.get("tool_call_id") == "call_1" for m in result)
        if has_result:
            has_parent = any(
                m.get("role") == "assistant"
                and any(tc.get("id") == "call_1" for tc in m.get("tool_calls", []))
                for m in result
            )
            assert has_parent, "tool_result kept without its tool_use parent — pair split"

    def test_drops_orphan_tool_result_when_parent_does_not_fit(self, counter):
        big_call = "y" * 800
        big_result = "z" * 500
        msgs = [
            {"role": "user", "content": big_call},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "bar", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_2", "content": big_result},
            {"role": "assistant", "content": "final"},
        ]
        result = fit_budget(msgs, counter.count("final"), counter)
        assert any(m.get("content") == "final" for m in result)
        assert not any(m.get("tool_call_id") == "call_2" for m in result), (
            "tool_result dropped, but its orphan parent may still be present"
        )
        assert not any(
            m.get("role") == "assistant"
            and any(tc.get("id") == "call_2" for tc in m.get("tool_calls", []))
            for m in result
        ), "tool_use dropped, but its orphan result may still be present"


class TestSummariserNoLLM:
    @pytest.fixture
    def summariser(self, cfg):
        return Summariser(None, cfg)

    def test_no_llm_returns_empty(self, summariser):
        import asyncio

        msgs = [{"role": "user", "content": "x" * 100}]
        assert asyncio.run(summariser.summarise(msgs)) == ""

    def test_empty_messages_returns_empty(self, summariser):
        import asyncio

        assert asyncio.run(summariser.summarise([])) == ""


class TestSummariserLLM:
    @pytest.fixture
    def llm(self):
        llm = AsyncMock()
        response = AsyncMock()
        response.content = '{"focus": "doing X", "done": ["step1"]}'
        response.stop_reason = "end_turn"
        response.input_tokens = 10
        response.output_tokens = 5
        llm.complete = AsyncMock(return_value=response)
        return llm

    @pytest.fixture
    def summariser(self, cfg, llm):
        return Summariser(llm, cfg)

    def test_llm_returns_raw_string_without_validation(self, summariser):
        import asyncio

        msgs = [{"role": "user", "content": "x" * 100}]
        result = asyncio.run(summariser.summarise(msgs))
        assert result == '{"focus": "doing X", "done": ["step1"]}'

    def test_llm_empty_content_returns_empty(self, cfg, summariser):
        import asyncio

        response = AsyncMock()
        response.content = ""
        response.stop_reason = "end_turn"
        response.input_tokens = 10
        response.output_tokens = 0
        summariser._llm.complete = AsyncMock(return_value=response)

        msgs = [{"role": "assistant", "content": "real decision with enough text here"}]
        assert asyncio.run(summariser.summarise(msgs)) == ""

    def test_llm_exception_returns_empty(self, cfg):
        import asyncio

        llm = AsyncMock()
        llm.complete = AsyncMock(side_effect=RuntimeError("LLM down"))
        summariser = Summariser(llm, cfg)

        msgs = [{"role": "assistant", "content": "real decision with enough text here"}]
        assert asyncio.run(summariser.summarise(msgs)) == ""


class TestPipelineStageSelection:
    @pytest.fixture
    def pipeline(self, cfg):
        return HistoryCompressor(
            [
                NoCompressionStage(),
                ToolCompressStage(),
                SummarizeStage(
                    recent_msgs=cfg.history_recent_msgs,
                    level=CompressionLevel.HISTORY_SUMMARY,
                ),
                SummarizeStage(
                    recent_msgs=cfg.topic_recent_msgs,
                    level=CompressionLevel.TOPIC_SUMMARY,
                ),
                EmergencyStage(),
            ]
        )

    def _ctx(self, cfg, counter):
        return StageContext(counter=counter, config=cfg, summariser=Summariser(None, cfg))

    @pytest.mark.parametrize(
        "ratio,expected_level",
        [
            (0.24, CompressionLevel.NONE),
            (0.25, CompressionLevel.TOOL_COMPRESS),
            (0.49, CompressionLevel.TOOL_COMPRESS),
            (0.69, CompressionLevel.TOOL_COMPRESS),
            (0.70, CompressionLevel.HISTORY_SUMMARY),
            (0.84, CompressionLevel.HISTORY_SUMMARY),
            (0.85, CompressionLevel.TOPIC_SUMMARY),
            (0.91, CompressionLevel.TOPIC_SUMMARY),
            (0.92, CompressionLevel.EMERGENCY),
            (0.99, CompressionLevel.EMERGENCY),
        ],
    )
    async def test_pipeline_selects_correct_stage(
        self, pipeline, cfg, counter, ratio, expected_level
    ):

        msgs = [{"role": "user", "content": "x"}]
        ctx = self._ctx(cfg, counter)
        _, level = await pipeline.run(msgs, 10_000, ctx, ratio)
        assert level == expected_level

    @pytest.mark.parametrize(
        "max_level,ratio,expected_level",
        [
            # ratio would select EMERGENCY (>=0.92), but max_level caps it.
            (CompressionLevel.TOOL_COMPRESS, 0.99, CompressionLevel.TOOL_COMPRESS),
            (CompressionLevel.HISTORY_SUMMARY, 0.99, CompressionLevel.HISTORY_SUMMARY),
            (CompressionLevel.TOPIC_SUMMARY, 0.99, CompressionLevel.TOPIC_SUMMARY),
            # max_level at or above the ratio-selected level is a no-op.
            (CompressionLevel.EMERGENCY, 0.99, CompressionLevel.EMERGENCY),
            # ratio selects HISTORY_SUMMARY (0.70-0.84), max_level=TOPIC allows it.
            (CompressionLevel.TOPIC_SUMMARY, 0.75, CompressionLevel.HISTORY_SUMMARY),
        ],
    )
    async def test_max_level_clamps_to_highest_stage_within_cap(
        self, pipeline, cfg, counter, max_level, ratio, expected_level
    ):
        """max_level should cap escalation at the HIGHEST stage <= cap, not the
        first stage <= cap (which is always NoCompressionStage)."""
        msgs = [{"role": "user", "content": "x"}]
        ctx = self._ctx(cfg, counter)
        _, level = await pipeline.run(msgs, 10_000, ctx, ratio, max_level=max_level)
        assert level == expected_level

    async def test_emergency_takes_last_two_messages(self, cfg, counter):

        pipeline = HistoryCompressor([EmergencyStage()])
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "fourth"},
            {"role": "user", "content": "fifth"},
        ]
        ctx = self._ctx(cfg, counter)
        result, level = await pipeline.run(msgs, 10_000, ctx, 0.99)
        assert level == CompressionLevel.EMERGENCY
        assert len(result) == 2
        assert result[0]["content"] == "fourth"


class TestEmergencyStageHistorySummary:
    def _ctx(self, cfg, counter):
        return StageContext(
            counter=counter,
            config=cfg,
            summariser=Summariser(None, cfg),
        )

    async def test_preserves_history_summary_outside_last_two(self, cfg, counter):
        stage = EmergencyStage()
        msgs = [
            {"role": "user", "content": '[HISTORY SUMMARY]\n{"focus":"investigating"}'},
            {"role": "user", "content": "old turn 1"},
            {"role": "assistant", "content": "old turn 2"},
            {"role": "user", "content": "recent user"},
            {"role": "assistant", "content": "recent assistant"},
        ]
        ctx = self._ctx(cfg, counter)
        result, level = await stage.apply(msgs, 10_000, ctx)
        assert level == CompressionLevel.EMERGENCY
        assert len(result) == 3
        assert result[0]["content"].startswith("[HISTORY SUMMARY]")
        assert result[-2]["content"] == "recent user"
        assert result[-1]["content"] == "recent assistant"

    async def test_no_summary_keeps_only_last_two(self, cfg, counter):
        stage = EmergencyStage()
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "fourth"},
        ]
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        assert len(result) == 2
        assert result[0]["content"] == "third"
        assert result[1]["content"] == "fourth"

    async def test_summary_inside_last_two_not_duplicated(self, cfg, counter):
        stage = EmergencyStage()
        summary_content = '[HISTORY SUMMARY]\n{"focus":"x"}'
        msgs = [
            {"role": "user", "content": "old 1"},
            {"role": "assistant", "content": "old 2"},
            {"role": "user", "content": summary_content},
            {"role": "assistant", "content": "most recent"},
        ]
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        assert len(result) == 2
        summary_count = sum(1 for m in result if m["content"].startswith("[HISTORY SUMMARY]"))
        assert summary_count == 1

    async def test_preserves_most_recent_summary_when_multiple(self, cfg, counter):
        stage = EmergencyStage()
        older_summary = '[HISTORY SUMMARY]\n{"focus":"old focus"}'
        newer_summary = '[HISTORY SUMMARY]\n{"focus":"new focus"}'
        msgs = [
            {"role": "user", "content": older_summary},
            {"role": "user", "content": "middle 1"},
            {"role": "user", "content": newer_summary},
            {"role": "user", "content": "recent user"},
            {"role": "assistant", "content": "recent assistant"},
        ]
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        assert result[0]["content"] == newer_summary
        assert result[-2]["content"] == "recent user"

    async def test_summary_at_extreme_budget_still_fits_when_possible(self, cfg, counter):
        stage = EmergencyStage()
        summary_content = "[HISTORY SUMMARY]\nsummary"
        msgs = [
            {"role": "user", "content": summary_content},
            {"role": "user", "content": "a" * 100},
            {"role": "assistant", "content": "b" * 100},
        ]
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        assert len(result) >= 1


class TestEmergencyStageActionsTaken:
    """The [ACTIONS TAKEN] block — prevents death loops after EMERGENCY."""

    def _ctx(self, cfg, counter):
        return StageContext(
            counter=counter,
            config=cfg,
            summariser=Summariser(None, cfg),
        )

    @staticmethod
    def _wire_tc(cid: str, name: str, args: dict) -> dict:
        """Production wire-shape tool_call (as written by agent_loop)."""
        return {
            "id": cid,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }

    async def test_actions_block_injected_when_tool_calls_in_history(self, cfg, counter):
        """History with tool_calls outside the kept tail → block injected."""
        stage = EmergencyStage()
        msgs = [
            {"role": "user", "content": "research GPT-4o vs Claude 3.5"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    self._wire_tc(
                        "call_1", "web_fetch", {"url": "https://example.com/gpt4o-bench"}
                    ),
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"ok": true, "chars": 5000}'},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    self._wire_tc(
                        "call_2", "web_fetch", {"url": "https://example.com/claude35-bench"}
                    ),
                ],
            },
            {"role": "tool", "tool_call_id": "call_2", "content": '{"ok": true, "chars": 5200}'},
            {"role": "assistant", "content": "now I will cross-check"},
            {"role": "user", "content": "ok proceed"},
        ]
        ctx = self._ctx(cfg, counter)
        result, level = await stage.apply(msgs, 10_000, ctx)
        assert level == CompressionLevel.EMERGENCY

        actions_msgs = [
            m for m in result if str(m.get("content", "")).startswith("[ACTIONS TAKEN]")
        ]
        assert len(actions_msgs) == 1
        body = actions_msgs[0]["content"]
        assert "web_fetch(url='https://example.com/gpt4o-bench')" in body
        assert "web_fetch(url='https://example.com/claude35-bench')" in body
        assert "ok=True" in body
        # Block sits at the head, before the recent tail.
        assert result[0]["content"].startswith("[ACTIONS TAKEN]")
        # Recent tail preserved.
        assert result[-1]["content"] == "ok proceed"
        assert result[-2]["content"] == "now I will cross-check"

    async def test_no_actions_block_when_no_tool_calls(self, cfg, counter):
        """History with no tool_calls → no block injected (existing behaviour)."""
        stage = EmergencyStage()
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
            {"role": "assistant", "content": "fourth"},
        ]
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        assert not any(str(m.get("content", "")).startswith("[ACTIONS TAKEN]") for m in result)
        assert len(result) == 2

    async def test_repeated_identical_calls_deduped_with_count(self, cfg, counter):
        """Same tool_call repeated N times → shown once with (xN) suffix."""
        stage = EmergencyStage()
        msgs: list[dict] = [{"role": "user", "content": "research"}]
        for i in range(3):
            cid = f"call_{i}"
            msgs.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        self._wire_tc(cid, "web_fetch", {"url": "https://example.com/gpt4o-bench"}),
                    ],
                }
            )
            msgs.append(
                {"role": "tool", "tool_call_id": cid, "content": '{"ok": true, "chars": 5000}'}
            )
        msgs.append({"role": "assistant", "content": "next"})
        msgs.append({"role": "user", "content": "go"})
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        actions_msgs = [
            m for m in result if str(m.get("content", "")).startswith("[ACTIONS TAKEN]")
        ]
        assert len(actions_msgs) == 1
        body = actions_msgs[0]["content"]
        # 3 identical fetches collapse to one line with (x3).
        assert "(x3)" in body
        assert "web_fetch(url='https://example.com/gpt4o-bench')" in body
        # Only one web_fetch line in the body besides the header.
        web_fetch_lines = [ln for ln in body.split("\n") if "web_fetch" in ln]
        assert len(web_fetch_lines) == 1


class TestBuildActionsTaken:
    """Unit tests for the _build_actions_taken helper."""

    def test_skips_tool_calls_already_in_kept_ids(self, counter):
        from prodagent.cognition.context.compression import _build_actions_taken

        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": '{"url": "https://a"}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"ok": true}'},
        ]
        # Both msgs are "kept" → no actions emitted.
        kept_ids = {id(m) for m in msgs}
        kept_tc_ids = {str(m.get("tool_call_id", "")) for m in msgs if "tool_call_id" in m}
        body = _build_actions_taken(
            msgs, kept_ids=kept_ids, kept_tool_call_ids=kept_tc_ids, counter=counter
        )
        assert body == ""

    def test_handles_simplified_tc_shape(self, counter):
        """Test-fixture shape {id, name, args} also works."""
        from prodagent.cognition.context.compression import _build_actions_taken

        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "name": "inspect_project", "args": {"project_id": 7}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"status": "running"}'},
        ]
        body = _build_actions_taken(msgs, kept_ids=set(), kept_tool_call_ids=set(), counter=counter)
        assert "inspect_project(project_id=7)" in body
        assert "status='running'" in body


class TestSummarizeStage:
    def _ctx(self, cfg, counter):
        return StageContext(
            counter=counter,
            config=cfg,
            summariser=Summariser(None, cfg),
        )

    async def test_summarize_stage_history_uses_recent_6(self, cfg, counter):

        stage = SummarizeStage(
            recent_msgs=cfg.history_recent_msgs, level=CompressionLevel.HISTORY_SUMMARY
        )
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(8)]
        ctx = self._ctx(cfg, counter)
        result, level = await stage.apply(msgs, 10_000, ctx)
        assert level == CompressionLevel.HISTORY_SUMMARY
        assert len(result) >= 6
        assert result[-6:] == msgs[-6:]

    async def test_summarize_stage_topic_uses_recent_4(self, cfg, counter):

        stage = SummarizeStage(
            recent_msgs=cfg.topic_recent_msgs, level=CompressionLevel.TOPIC_SUMMARY
        )
        msgs = [{"role": "user", "content": f"msg{i}"} for i in range(8)]
        ctx = self._ctx(cfg, counter)
        result, level = await stage.apply(msgs, 10_000, ctx)
        assert level == CompressionLevel.TOPIC_SUMMARY
        assert len(result) >= 4
        assert result[-4:] == msgs[-4:]

    async def test_summarize_stage_summary_message_role_is_user(self, cfg, counter):

        stage = SummarizeStage(recent_msgs=2, level=CompressionLevel.HISTORY_SUMMARY)
        msgs = [
            {"role": "user", "content": "tool result with enough text here for snippet"},
            {"role": "assistant", "content": "decision with enough text here too yes"},
            {"role": "user", "content": "recent1"},
            {"role": "assistant", "content": "recent2"},
        ]
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        summary_msgs = [
            m for m in result if str(m.get("content", "")).startswith("[HISTORY SUMMARY]")
        ]
        if summary_msgs:
            assert summary_msgs[0]["role"] == "user"


class TestSafeTailStart:
    def test_boundary_pointing_at_tool_result_walks_back_to_parent(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r"},
            {"role": "assistant", "content": "next"},
            {"role": "user", "content": "tail"},
        ]
        # naive len-2 = 3 points at the assistant "next" — no tool at boundary.
        assert safe_tail_start(msgs, 2) == 3

    def test_boundary_walks_back_through_multiple_results(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}, {"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
            {"role": "assistant", "content": "next"},
        ]
        # naive len-2 = 2 points at tool(c2) → walk back to the assistant parent.
        assert safe_tail_start(msgs, 2) == 0

    def test_boundary_not_at_tool_result_unchanged(self):
        msgs = [{"role": "user", "content": f"m{i}"} for i in range(8)]
        assert safe_tail_start(msgs, 6) == 2

    def test_recent_msgs_exceeding_length_clamps_to_zero(self):
        msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
        assert safe_tail_start(msgs, 10) == 0

    def test_empty_messages(self):
        assert safe_tail_start([], 6) == 0


class TestSummarizeStageToolPairSafety:
    def _ctx(self, cfg, counter):
        return StageContext(
            counter=counter,
            config=cfg,
            summariser=Summariser(None, cfg),
        )

    async def test_boundary_on_tool_result_does_not_orphan_parent(self, cfg, counter):
        """Regression (crash shape): the naive ``-recent_msgs`` boundary lands
        on a tool result whose assistant(tool_calls) parent is in the
        summarized prefix. The old code kept the orphaned tool result →
        HTTP 400 "tool must be a response to a preceding message with
        'tool_calls'". The fix walks the boundary back to include the parent."""
        stage = SummarizeStage(recent_msgs=2, level=CompressionLevel.HISTORY_SUMMARY)
        msgs = [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "first result"},
            {"role": "user", "content": "recent"},
        ]
        # naive boundary = len-2 = 2 → msgs[2] IS a tool result.
        assert safe_tail_start(msgs, 2) == 1
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        _assert_no_orphan_tool_results(result)
        # pair preserved verbatim, not dropped.
        assert any(m.get("tool_call_id") == "call_1" for m in result)
        assert any(
            m.get("role") == "assistant"
            and any(tc.get("id") == "call_1" for tc in m.get("tool_calls", []))
            for m in result
        )

    async def test_boundary_on_tool_result_keeps_parent_in_recent(self, cfg, counter):
        stage = SummarizeStage(recent_msgs=6, level=CompressionLevel.HISTORY_SUMMARY)
        msgs = [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result-1"},
            {"role": "user", "content": "m1"},
            {"role": "assistant", "content": "note1"},
            {"role": "user", "content": "m2"},
            {"role": "assistant", "content": "note2"},
            {"role": "user", "content": "m3"},
        ]
        # naive boundary = len-6 = 2 → msgs[2] IS a tool result → the old code
        # orphaned it (its assistant parent sits in the summarized prefix).
        assert safe_tail_start(msgs, 6) == 1
        ctx = self._ctx(cfg, counter)
        result, _ = await stage.apply(msgs, 10_000, ctx)
        _assert_no_orphan_tool_results(result)
        # the pair is preserved verbatim (not truncated away).
        assert any(m.get("tool_call_id") == "call_1" for m in result)
        assert any(
            m.get("role") == "assistant"
            and any(tc.get("id") == "call_1" for tc in m.get("tool_calls", []))
            for m in result
        )


class TestEmergencyStageToolPairSafety:
    def _ctx(self, cfg, counter):
        return StageContext(
            counter=counter,
            config=cfg,
            summariser=Summariser(None, cfg),
        )

    async def test_emergency_last_two_keeps_tool_pair_intact(self, cfg, counter):
        """The naive ``messages[-2:]`` tail split a tool pair when the last two
        messages were [tool_result, assistant]."""
        stage = EmergencyStage()
        msgs = [
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "assistant", "content": "next"},
        ]
        ctx = self._ctx(cfg, counter)
        result, level = await stage.apply(msgs, 10_000, ctx)
        assert level == CompressionLevel.EMERGENCY
        _assert_no_orphan_tool_results(result)
        # parent + result both present → pair not split.
        assert any(m.get("tool_call_id") == "call_1" for m in result)
        assert any(
            m.get("role") == "assistant"
            and any(tc.get("id") == "call_1" for tc in m.get("tool_calls", []))
            for m in result
        )
