"""Structured output — three lines of defence against "almost-JSON".

Ask a model to return only JSON and it will still answer with prose, code
fences, or both. Rather than hoping harder, this module defends in depth:
① a balanced-brace scanner (not a regex — JSON nests arbitrarily, and braces
inside strings must not count) that peels fences and finds the first
parseable object; ② extract → ``json.loads`` → pydantic validation folded
into one ``StructuredOutputError``; ③ the self-correction loop — validation
failures are appended back into the conversation so the *model* fixes its
own output, the same philosophy as tool errors returning structured results:
model mistakes are routine; the framework's job is a chance to correct, not
a crash.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from prodagent.kernel.types import LLMResponse, MessageList
    from prodagent.llm import LLMClient, LLMConfig

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    """The LLM response could not be parsed or validated against the schema."""


def extract_json_object(text: str) -> str:
    """Pull the first valid JSON object/array out of model output."""
    # Models wrap JSON in prose and markdown fences despite instructions;
    # scan for the first balanced object/array instead of assuming clean
    # output — and keep scanning past prose that merely looks balanced.
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    search_from = 0
    first_candidate: str | None = None
    while True:
        obj_start = text.find("{", search_from)
        arr_start = text.find("[", search_from)
        if obj_start == -1 and arr_start == -1:
            if first_candidate is not None:
                return first_candidate
            raise ValueError("No JSON object or array found in output")
        if obj_start == -1:
            start = arr_start
            open_ch, close_ch = "[", "]"
        elif arr_start == -1:
            start = obj_start
            open_ch, close_ch = "{", "}"
        else:
            start = min(obj_start, arr_start)
            if start == obj_start:
                open_ch, close_ch = "{", "}"
            else:
                open_ch, close_ch = "[", "]"

        depth = 0
        in_string = False
        escape = False
        end = -1
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False  # previous char was "\", this one is literal
                continue
            if c == "\\":
                escape = True  # next char is escaped — skip its meaning
                continue
            if c == '"':
                in_string = not in_string  # string boundaries toggle string mode
                continue
            if in_string:
                continue  # braces inside strings must NOT count toward depth
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    end = i  # first balanced close — candidate found
                    break
        if end == -1:
            if first_candidate is not None:
                return first_candidate  # nothing later balanced either — settle for the first
            raise ValueError(f"Unmatched {open_ch}{close_ch} in output")

        candidate = text[start : end + 1]
        if first_candidate is None:
            first_candidate = candidate  # remember the first balanced span as fallback
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            # Balanced braces but invalid JSON (likely a Python-dict repr in prose); try next.
            search_from = start + 1


def parse_json_as(text: str, model: type[T]) -> T:
    """Extract + parse + validate against pydantic model. Raises StructuredOutputError."""
    try:
        extracted = extract_json_object(text)
    except ValueError as exc:
        raise StructuredOutputError(f"No JSON object found in output: {exc}") from exc
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"JSON decode failed: {exc}") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"Schema validation failed for {model.__name__}: {exc}"
        ) from exc


async def complete_structured(
    llm: LLMClient,
    messages: MessageList,
    model: type[T],
    *,
    system: str | list[dict[str, Any]] = "",
    config: LLMConfig | None = None,
    max_retries: int = 2,
) -> tuple[T, LLMResponse]:
    """Call *llm* until its response parses+validates as ``model``, or retries exhaust.

    Each retry appends the validation error so the model can self-correct.
    """
    from prodagent.llm import stream_text

    conversation: MessageList = list(messages)
    last_error: StructuredOutputError | None = None
    last_response: LLMResponse | None = None

    for attempt in range(max_retries + 1):
        response, text = await stream_text(llm, conversation, system=system, config=config)
        last_response = response
        try:
            parsed = parse_json_as(text, model)
        except StructuredOutputError as exc:
            last_error = exc
            logger.warning(
                "Structured output attempt %d/%d failed: %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )
            # Self-correction: append the failed answer + the validator's
            # complaint so the next call fixes *this* error, not a generic one.
            conversation = conversation + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "Your previous response failed validation:\n"
                        f"{exc}\n\n"
                        "Return ONLY a JSON object matching this schema. "
                        "No prose, no markdown fences."
                    ),
                },
            ]
            continue
        return parsed, response

    assert last_error is not None and last_response is not None
    raise last_error
