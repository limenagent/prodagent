from __future__ import annotations

import pytest
from pydantic import BaseModel

from prodagent.core.types import LLMResponse
from prodagent.llm.base import LLMConfig
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.llm.structured_output import (
    StructuredOutputError,
    complete_structured,
    parse_json_as,
)


class _PlanSchema(BaseModel):
    steps: list[dict]
    summary: str = ""


def test_parse_json_as_validates_clean_json() -> None:
    text = '{"steps": [{"id": "s1"}], "summary": "ok"}'
    parsed = parse_json_as(text, _PlanSchema)
    assert parsed.steps == [{"id": "s1"}]
    assert parsed.summary == "ok"


def test_parse_json_as_tolerates_markdown_fence_and_prose() -> None:
    text = 'Here is the plan:\n```json\n{"steps": [], "summary": "empty"}\n```\nDone.'
    parsed = parse_json_as(text, _PlanSchema)
    assert parsed.summary == "empty"
    assert parsed.steps == []


def test_parse_json_as_raises_on_missing_required_field() -> None:
    text = '{"summary": "no steps field"}'
    with pytest.raises(StructuredOutputError) as exc_info:
        parse_json_as(text, _PlanSchema)
    assert "Schema validation failed" in str(exc_info.value)


def test_parse_json_as_raises_on_no_json() -> None:
    with pytest.raises(StructuredOutputError) as exc_info:
        parse_json_as("just prose, no json here", _PlanSchema)
    assert "No JSON object found" in str(exc_info.value)


@pytest.mark.asyncio
async def test_complete_structured_retries_then_succeeds() -> None:
    llm = FakeLLMAdapter(
        responses=[
            LLMResponse(content='{"summary": "missing steps"}', stop_reason="end_turn"),
            LLMResponse(content='{"steps": [], "summary": "fixed"}', stop_reason="end_turn"),
        ]
    )
    parsed, _ = await complete_structured(
        llm,
        [{"role": "user", "content": "make a plan"}],
        _PlanSchema,
        config=LLMConfig(model="fake"),
        max_retries=2,
    )
    assert parsed.summary == "fixed"


@pytest.mark.asyncio
async def test_complete_structured_raises_after_retries_exhausted() -> None:
    llm = FakeLLMAdapter(
        responses=[
            LLMResponse(content='{"summary": "still no steps"}', stop_reason="end_turn"),
            LLMResponse(content='{"summary": "still no steps"}', stop_reason="end_turn"),
            LLMResponse(content='{"summary": "still no steps"}', stop_reason="end_turn"),
        ]
    )
    with pytest.raises(StructuredOutputError):
        await complete_structured(
            llm,
            [{"role": "user", "content": "make a plan"}],
            _PlanSchema,
            config=LLMConfig(model="fake"),
            max_retries=2,
        )
