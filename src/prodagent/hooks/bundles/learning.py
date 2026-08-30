"""LearningHooks — wire the closed learning loop into SESSION_END."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from prodagent.kernel.bus import HookEvent
from prodagent.ports.persistence import ExperienceOutcome, ExperienceRecord

if TYPE_CHECKING:
    from prodagent.base.config import FrameworkConfig
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun
    from prodagent.ports.persistence import ExperienceStore
    from prodagent.skills.registry import SkillContent, SkillRegistry
    from prodagent.skills.skill_synthesizer import SkillSynthesizer

logger = logging.getLogger(__name__)

__all__ = ["LearningHooks"]


class LearningHooks:
    """The learning cartridge: record every finished run as an experience,
    then (in the background, off the session's critical path) let the
    synthesizer distil corroborated successes into skill patches."""

    def __init__(
        self,
        synthesizer: SkillSynthesizer | None = None,
        *,
        registry: SkillRegistry,
        store: ExperienceStore | None = None,
        framework_config: FrameworkConfig | None = None,
        watch_tags: list[str] | None = None,
    ) -> None:
        if store is None and framework_config is not None:
            from prodagent.backends.factory import resolve_experience

            store = resolve_experience(framework_config)
        if store is None:
            raise ValueError(
                "LearningHooks requires either an explicit store or a framework_config "
                "to lazy-resolve one."
            )
        # synthesizer lazy-resolves its aux LLM from framework_config so callers
        # don't have to wire SkillSynthesizer(llm, registry) themselves. Passing
        # `store` here is what activates SkillSynthesizer's stricter overwrite
        # gate (corroboration count + cooldown) — see its docstring.
        if synthesizer is None and framework_config is not None:
            from prodagent.backends.factory import resolve_llm
            from prodagent.skills.skill_synthesizer import SkillSynthesizer

            synthesizer = SkillSynthesizer(resolve_llm(framework_config), registry, store=store)
        if synthesizer is None:
            raise ValueError(
                "LearningHooks requires either an explicit synthesizer or a "
                "framework_config to lazy-resolve one."
            )
        self._store = store
        self._synthesizer = synthesizer
        self._registry = registry
        self._watch_tags = list(watch_tags) if watch_tags else []
        self._hooks: HookRegistry | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def attach(self, hooks: HookRegistry) -> None:
        """One listener: SESSION_END fans out the loop, everything else is
        the background task's business."""
        self._hooks = hooks
        hooks.register_event(HookEvent.SESSION_END, self._on_session_end)

    async def flush(self, timeout: float = 30.0) -> None:
        """Drain in-flight synthesis at shutdown — bounded wait, and stragglers
        are left running rather than cancelled (a half-written skill file is
        worse than a slow exit)."""
        if not self._tasks:
            return
        pending = list(self._tasks)
        try:
            await asyncio.wait_for(
                asyncio.gather(*pending, return_exceptions=True),
                timeout=timeout,
            )
        except TimeoutError:
            still = [t for t in pending if not t.done()]
            logger.warning(
                "LearningHooks.flush: %d task(s) still running after %.1fs — "
                "leaving them to self-clean (not cancelling to avoid half-written skills)",
                len(still),
                timeout,
            )

    async def _on_session_end(self, *, run: AgentRun | None = None, **_: Any) -> None:
        """Record + spawn the background patch. Subordinate children are
        skipped — their transcript folds into the parent, whose own end
        carries the whole story."""
        if run is None:
            return
        from prodagent.kernel.state import is_child_subordinate

        if is_child_subordinate(run):
            return
        # Synthesis is an LLM round-trip; the terminal event must not wait for
        # it. It runs in the background and flush() drains at exit.
        task = asyncio.create_task(self._safely_run_loop(run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safely_run_loop(self, run: AgentRun) -> None:
        # Best-effort: swallow exceptions.
        try:
            rec = ExperienceRecord.from_run(run)
            await self._store.record(rec)

            if not self._should_patch(rec):
                return

            result = await self._synthesizer.maybe_synthesize(rec)
            if result.skill is not None:
                logger.info("LearningHooks: skill %r patched/created", result.skill.card.name)
                await _notify_skill_synthesised(
                    result.skill, registry=self._registry, hooks=self._hooks
                )
            elif result.failure:
                await self._emit(action="skipped", detail=result.failure)
        except Exception as exc:
            logger.exception("LearningHooks: background patch failed")
            await self._emit(action="skipped", detail=f"Background patch raised: {exc!r}")

    async def _emit(self, *, action: str, name: str = "", detail: str = "") -> None:
        if self._hooks is None:
            return
        await self._hooks.fire(
            HookEvent.LEARNING_SYNTHESIZE,
            action=action,
            name=name,
            detail=detail,
        )

    def _should_patch(self, rec: ExperienceRecord) -> bool:
        # Patch only on SUCCESS and only when tags overlap (or watch_tags is empty).
        if rec.outcome != ExperienceOutcome.SUCCESS:
            return False
        if not self._watch_tags:
            return True
        return bool(set(rec.tags) & set(self._watch_tags))


async def _notify_skill_synthesised(
    skill: SkillContent,
    *,
    registry: SkillRegistry,
    hooks: HookRegistry | None,
) -> None:
    path = registry.path_for(skill.card.name)
    detail = f"desc={skill.card.description} tags={skill.card.tags}"
    if path:
        detail += f" file={path}"
    if hooks is not None:
        await hooks.fire(
            HookEvent.LEARNING_SYNTHESIZE,
            action="patched",
            name=skill.card.name,
            detail=detail,
        )
    for i, c in enumerate(skill.contradictions, 1):
        logger.warning(
            "contradiction %d: old=%s new=%s resolution=%s",
            i,
            c.get("old", "")[:160],
            c.get("new", "")[:160],
            c.get("resolution", "")[:160],
        )
