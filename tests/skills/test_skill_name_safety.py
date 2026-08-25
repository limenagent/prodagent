"""LLM-synthesised skill names must never reach the filesystem unsanitised.

The synthesizer feeds ``data["name"]`` from an LLM's JSON straight into
``skills_dir / f"{name}.md"`` — ``../../evil`` wrote outside the skills
directory. Two layers: the synthesizer sanitizes at ingestion; the registry
refuses to persist/archive names that could traverse regardless of source.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.skills.registry import SkillCard, SkillContent, SkillRegistry
from prodagent.skills.skill_synthesizer import _coerce_tags, _sanitize_name

if TYPE_CHECKING:
    from pathlib import Path


class TestSanitizeName:
    def test_traversal_is_neutralised(self):
        assert "/" not in _sanitize_name("../../evil")
        assert "\\" not in _sanitize_name("..\\..\\evil")
        assert _sanitize_name("../../evil") != ".."

    def test_normal_names_survive_untouched(self):
        assert _sanitize_name("deploy-runbook") == "deploy-runbook"
        assert _sanitize_name("api_retry.v2") == "api_retry.v2"

    def test_empty_and_garbage_fall_back(self):
        assert _sanitize_name("") == "synthesised-skill"
        assert _sanitize_name("???") == "synthesised-skill"
        assert _sanitize_name("///") == "synthesised-skill"


class TestCoerceTags:
    def test_bare_string_is_wrapped_not_char_split(self):
        tags = _coerce_tags("deploy", "deploy")
        assert tags == ["deploy"]

    def test_non_string_items_are_dropped_and_deduped(self):
        tags = _coerce_tags(["a", 2, None, {"x": 1}, "a"], "primary")
        assert tags == ["a", "2"]

    def test_invalid_type_falls_back_to_primary(self):
        assert _coerce_tags(42, "primary") == ["primary"]


class TestRegistryDiskBoundary:
    def _content(self, name: str) -> SkillContent:
        return SkillContent(
            card=SkillCard(name=name, description="d", tags=["t"]),
            full_doc="# doc",
        )

    def test_registry_refuses_to_persist_traversing_name(self, tmp_path: Path):
        registry = SkillRegistry(skills_dir=tmp_path)
        path = registry._persist(self._content("../../evil"))
        assert path is None
        assert list(tmp_path.rglob("*.md")) == []  # nothing written anywhere

    def test_registry_still_persists_safe_names(self, tmp_path: Path):
        registry = SkillRegistry(skills_dir=tmp_path)
        path = registry._persist(self._content("safe-skill"))
        assert path is not None and path.parent == tmp_path

    def test_archive_skips_unsafe_name(self, tmp_path: Path):
        registry = SkillRegistry(skills_dir=tmp_path)
        assert registry._history_dir("../escape") is None
