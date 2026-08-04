"""SkillSynthesizer — distil reusable procedures from successful agent sessions."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prodagent.evaluation.learning.experience import (
    ExperienceOutcome,
    ExperienceRecord,
)
from prodagent.evaluation.skills.registry import SkillCard, SkillContent, SkillRegistry
from prodagent.llm.base import LLMClient, LLMConfig, noop_chunk
from prodagent.llm.structured_output import extract_json_object

if TYPE_CHECKING:
    from prodagent.core.types import MessageList

logger = logging.getLogger(__name__)

_HEAD_CHARS: int = 1_500
_TAIL_CHARS: int = 500
_MAX_TOOL_RESULT_CHARS: int = _HEAD_CHARS + _TAIL_CHARS
_MAX_SESSION_CHARS: int = 20_000
_SKILL_SECTIONS: tuple[tuple[str, str], ...] = (
    ("when_to_use", "## When to use"),
    ("procedure", "## Procedure"),
    ("pitfalls", "## Pitfalls"),
    ("verification", "## Verification"),
)

_SYNTH_TAG_OVERLAP = 1

_SYNTHESIS_SYSTEM = """\
You are a process analyst who distils reusable procedures from real agent \
session transcripts.

Each transcript is the complete conversation log for one successful task \
resolution: the original user task, the agent's reasoning turns, every tool \
call (identified by "Tool 'name' result:" lines), every tool result, and the \
final output.

Your output is stored as a Skill document and injected into future agent \
prompts so they can handle the same class of task faster and more reliably. \
Write for an AI agent that already knows the tool names: focus on WHEN and \
WHY, not just WHAT.

Do not assume any particular domain. Extract only the generalisable procedure \
that the transcripts actually demonstrate — do not invent steps that are not \
supported by the evidence.

Return valid JSON only — no preamble, no markdown fences."""

_SYNTHESIS_TEMPLATE = """\
## Synthesis task
The following {n} successful session(s) each resolved a similar class of task.
Each transcript is the COMPLETE conversation: user task → agent reasoning → \
tool calls with results → final output.

## Session transcripts
{transcripts}

## Your task
Identify the generalised procedure demonstrated across these sessions. Extract:
- When this procedure applies (the recognisable task signature)
- What to establish or check before acting, and why
- The action sequence (exact steps in order, with the reasoning and any guard
  conditions at each step)
- The decision points (what observation determines which branch)
- How to confirm the task is genuinely complete (not just superficially done)

Return a JSON object with exactly these fields:
{{
  "name": "kebab-case-procedure-name",
  "description": "One sentence: when to use this procedure (max 120 chars)",
  "version": "1.0",
  "tags": ["tag1", "tag2"],
  "when_to_use": "The task signature that should trigger this procedure.",
  "procedure": "Numbered step-by-step procedure as markdown. Include exact tool names, key parameters, and the decision logic / guard condition at each step.",
  "pitfalls": "Common mistakes the transcripts reveal — things that nearly went wrong or that required correction.",
  "verification": "Exact check(s) that confirm the task is genuinely complete, not just symptom-free."
}}"""


_SKILL_UPDATE_SYSTEM = """\
You are a process analyst who maintains a library of reusable runbooks.

You will receive an EXISTING skill document plus a NEW successful session \
transcript. Your job is to PATCH the skill: incorporate new steps, \
refinements, or pitfalls — while actively detecting contradictions between \
the new transcript and the existing skill.

This is a rolling accumulator: the skill file encodes distilled knowledge \
from prior sessions. The new transcript is a delta; the patched skill must \
preserve everything still valid and only add conditional branches where the \
new evidence conflicts.

Write for an AI agent that already knows the tool names: focus on WHEN and \
WHY, not just WHAT.

Do not assume any particular domain. Extract only the generalisable \
procedure that the transcript actually demonstrates — do not invent steps \
that are not supported by the evidence.

Return valid JSON only — no preamble, no markdown fences."""

_SKILL_UPDATE_TEMPLATE = """\
## Existing skill (current version)
Name: {existing_name}
Version: {existing_version}
Tags: {existing_tags}

{existing_doc}

## New session transcript to absorb
One successful session that resolved a task covered by this skill.
The transcript is the COMPLETE conversation: user task → agent reasoning → \
tool calls with results → final output.

{transcripts}

## Your task — PATCH, do not regenerate

STEP 1 — SCAN FOR CONTRADICTIONS. Go through each existing step in the \
procedure and each existing pitfall. For each, ask: does the new transcript \
show evidence that contradicts it (different order, different first action, \
different guard condition, different root-cause assumption)? Record every \
contradiction in the `contradictions_detected` array.

STEP 2 — PATCH THE PROCEDURE. Produce the updated procedure:
- If no contradiction: merge new steps into the existing sequence where they \
fit; add missing guards; keep existing content intact.
- If contradiction: keep BOTH the old and new advice as explicit conditional \
branches in the procedure (e.g. "When <observation_A>: <old_action>. When \
<observation_B> (seen in new evidence): <new_action>."). NEVER silently \
overwrite the old advice.

STEP 3 — PATCH PITFALLS + VERIFICATION. Add any new pitfalls the transcript \
reveals. Tighten verification only if the transcript exposes a false-positive \
check.

STEP 4 — METADATA. Keep the existing name and tags (you may add tags if the \
transcript reveals an additional dimension). Bump the minor version \
({existing_version} → next minor).

Return a JSON object with exactly these fields:
{{
  "name": "{existing_name}",
  "description": "One sentence: when to use this procedure (max 120 chars)",
  "version": "next minor version after {existing_version}",
  "tags": ["...existing plus any new..."],
  "when_to_use": "The task signature that should trigger this procedure.",
  "procedure": "Numbered step-by-step procedure as markdown. Where contradictions exist, use conditional branches (When X: ... When Y: ...).",
  "pitfalls": "Common mistakes — existing plus any new from this transcript.",
  "verification": "Exact check(s) that confirm the task is genuinely complete.",
  "contradictions_detected": [
    {{
      "old": "the existing advice that conflicts",
      "new": "the new evidence from this transcript",
      "resolution": "how the patched procedure resolves it (the conditional branch)"
    }}
  ]
}}"""


def _format_session_transcript(rec: ExperienceRecord) -> str:
    """Render a session's conversation for the synthesis LLM.

    Falls back to a thin metadata summary (tools + output preview) when
    ``session_transcript`` is empty.
    """
    header = [
        f"=== Session [{rec.outcome.value.upper()}] ===",
        f"Task: {rec.task}",
        f"Turns: {rec.turn_count}  Cost: ${rec.cost_usd:.4f}  Elapsed: {rec.elapsed_seconds:.1f}s",
        "",
    ]

    if not rec.session_transcript:
        tools = " → ".join(rec.tool_sequence[:8]) if rec.tool_sequence else "(none)"
        output_preview = (rec.final_output or "")[:400].replace("\n", " ")
        short_body = [
            f"Tools used: {tools}",
            f"Final output: {output_preview}",
            "(no full transcript available)",
        ]
        return "\n".join(header + short_body)

    body: list[str] = []
    for msg in rec.session_transcript:
        role = str(msg.get("role", "?")).upper()
        content = msg.get("content", "")

        # Defensively flatten list content (engine.py always writes string
        # content, but guard anyway).
        if isinstance(content, list):
            content = " ".join(
                b.get("text", str(b)) if isinstance(b, dict) else str(b) for b in content
            )
        content = str(content)

        if (
            role == "USER"
            and content.startswith("Tool '")
            and len(content) > _MAX_TOOL_RESULT_CHARS
        ):
            content = (
                content[:_HEAD_CHARS] + "\n...(middle truncated)...\n" + content[-_TAIL_CHARS:]
            )

        body.append(f"[{role}] {content}")
        body.append("")

    full = "\n".join(header + body)
    if len(full) > _MAX_SESSION_CHARS:
        head_budget = int(_MAX_SESSION_CHARS * 0.7)
        tail_budget = _MAX_SESSION_CHARS - head_budget
        full = (
            full[:head_budget]
            + "\n...(session middle truncated — size limit reached)...\n"
            + full[-tail_budget:]
        )
    return full


@dataclass
class SynthesisResult:
    """Outcome of one synthesis attempt — carries the skill OR the failure reason.

    Returning the failure inline (rather than stashing it on ``self.last_failure``)
    avoids a race where concurrent ``maybe_synthesize`` calls overwrite each
    other's failure reason before the caller reads it.
    """

    skill: SkillContent | None = None
    failure: str = ""

    @property
    def ok(self) -> bool:
        return self.skill is not None


class SkillSynthesizer:
    """Rolling-accumulator skill patcher."""

    _MAX_ATTEMPTS = 3

    def __init__(
        self,
        llm: LLMClient,
        registry: SkillRegistry,
        *,
        config: LLMConfig | None = None,
        max_attempts: int = _MAX_ATTEMPTS,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._config = config or LLMConfig(temperature=0.0, max_tokens=32768, timeout_seconds=180.0)
        self._max_attempts = max(1, max_attempts)

    async def maybe_synthesize(
        self,
        seed_record: ExperienceRecord,
    ) -> SynthesisResult:
        """Patch (or create) a skill from a single successful run."""
        if seed_record.outcome != ExperienceOutcome.SUCCESS:
            return SynthesisResult()

        existing_name = self._registry.find_by_tags(
            seed_record.tags, min_tag_overlap=_SYNTH_TAG_OVERLAP
        )
        existing = self._registry.get(existing_name) if existing_name else None
        primary_tag = seed_record.tags[0] if seed_record.tags else ""

        result = await self.synthesize_from(
            [seed_record], primary_tag=primary_tag, existing=existing
        )
        if result.skill is not None:
            self._register_or_merge(result.skill)
            contra = (
                f" — {len(result.skill.contradictions)} contradiction(s) flagged"
                if result.skill.contradictions
                else ""
            )
            logger.info(
                "SkillSynthesizer: %s skill %r%s",
                "patched" if existing else "synthesised",
                result.skill.card.name,
                contra,
            )
        return result

    def _register_or_merge(self, skill: SkillContent) -> None:
        """Register *skill*, updating an existing near-duplicate in place."""
        existing_name = self._registry.find_similar(skill.card)
        if existing_name is not None and existing_name != skill.card.name:
            prior = self._registry.get(existing_name)
            skill.card.name = existing_name
            if prior is not None:
                skill.card.version = _bump_version(prior.card.version)
        self._registry.register(skill)

    async def synthesize_from(
        self,
        records: list[ExperienceRecord],
        *,
        primary_tag: str = "",
        existing: SkillContent | None = None,
    ) -> SynthesisResult:
        """Unconditionally synthesise (or patch) a Skill from *records*."""
        if not records:
            return SynthesisResult()

        transcripts = "\n---\n".join(_format_session_transcript(r) for r in records)
        if existing is not None:
            prompt = _SKILL_UPDATE_TEMPLATE.format(
                existing_name=existing.card.name,
                existing_version=existing.card.version,
                existing_tags=", ".join(existing.card.tags),
                existing_doc=existing.full_doc,
                n=len(records),
                transcripts=transcripts,
            )
            system = _SKILL_UPDATE_SYSTEM
        else:
            prompt = _SYNTHESIS_TEMPLATE.format(n=len(records), transcripts=transcripts)
            system = _SYNTHESIS_SYSTEM
        messages: MessageList = [{"role": "user", "content": prompt}]

        last_exc: Exception | None = None
        for attempt in range(self._max_attempts):
            try:
                response = await self._llm.complete(
                    messages, system=system, config=self._config, on_chunk=noop_chunk
                )
                break
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "SkillSynthesizer: LLM attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_attempts,
                    exc,
                )
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(0.1 * (2**attempt))
        else:
            logger.exception(
                "SkillSynthesizer: LLM call failed after %d attempts", self._max_attempts
            )
            return SynthesisResult(failure=f"LLM call raised: {last_exc!r}")

        raw = response.content or response.reasoning_content
        if not raw.strip():
            return SynthesisResult(
                failure=(
                    f"LLM returned empty content (stop_reason={response.stop_reason}, "
                    f"model={response.model}, input_tokens={response.input_tokens}, "
                    f"output_tokens={response.output_tokens})"
                )
            )
        return self._parse_skill(raw, primary_tag=primary_tag)

    def _parse_skill(self, raw: str, *, primary_tag: str) -> SynthesisResult:
        """Parse LLM JSON output into a SkillContent."""
        raw = raw.strip()
        try:
            extracted = extract_json_object(raw)
        except ValueError as exc:
            return SynthesisResult(
                failure=(
                    f"extract_json_object failed: {exc} (len={len(raw)}, "
                    f"head={raw[:200]!r}, tail={raw[-200:] if len(raw) > 200 else ''!r})"
                )
            )
        try:
            data: dict[str, Any] = json.loads(extracted)
        except json.JSONDecodeError as exc:
            return SynthesisResult(
                failure=(
                    f"json.loads failed after extraction: {exc} (raw_len={len(raw)}, "
                    f"extracted_len={len(extracted)}, extracted_head={extracted[:200]!r}, "
                    f"extracted_tail={extracted[-200:] if len(extracted) > 200 else ''!r})"
                )
            )

        if not isinstance(data, dict):
            return SynthesisResult(
                failure=(
                    f"LLM returned JSON {type(data).__name__}, expected object "
                    f"(extracted_head={extracted[:200]!r})"
                )
            )

        name = str(data.get("name", f"synthesised-{primary_tag}"))
        description = str(data.get("description", ""))[:120]
        version = str(data.get("version", "1.0"))
        tags = data.get("tags", [primary_tag] if primary_tag else [])
        sections = [f"{header}\n{data[key]}" for key, header in _SKILL_SECTIONS if data.get(key)]
        full_doc = "\n\n".join(sections) if sections else "(synthesised — no procedure)"

        raw_contras_val = data.get("contradictions_detected")
        raw_contras: list[Any] = raw_contras_val if isinstance(raw_contras_val, list) else []
        contradictions = [
            {k: str(c.get(k, "")) for k in ("old", "new", "resolution")}
            for c in raw_contras
            if isinstance(c, dict)
        ]

        card = SkillCard(name=name, description=description, version=version, tags=tags)
        return SynthesisResult(
            skill=SkillContent(card=card, full_doc=full_doc, contradictions=contradictions)
        )


def _bump_version(version: str) -> str:
    """Increment the minor component of a ``major.minor`` version string."""
    try:
        major, _, minor = version.partition(".")
        return f"{int(major)}.{int(minor or 0) + 1}"
    except (ValueError, TypeError):
        return "1.1"
