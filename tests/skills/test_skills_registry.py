from __future__ import annotations

import tempfile
from pathlib import Path

from prodagent.skills.registry import SkillRegistry

_SKILL_WITH_FRONTMATTER = """\
---
name: oom_kill
description: Handle OOMKilled pods
version: "1.1"
tags: [oom, context, pod]
tools: [restart_pod, get_pod_status]
---

## OOM Kill Runbook

When a pod is OOMKilled, context limit was exceeded.

Steps:
1. Check pod status
2. Review context metrics
3. Restart the pod
"""

_SKILL_NO_FRONTMATTER = """\
## High CPU Runbook

Scale the deployment to add capacity.
"""


def _write_skill(directory: Path, filename: str, content: str) -> Path:
    path = directory / filename
    path.write_text(content, encoding="utf-8")
    return path


def test_from_dir_loads_skills():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        _write_skill(d, "oom_kill.md", _SKILL_WITH_FRONTMATTER)
        _write_skill(d, "high_cpu.md", _SKILL_NO_FRONTMATTER)

        reg = SkillRegistry.from_dir(d)
        names = [c.name for c in reg.cards]
        assert "oom_kill" in names
        assert "high_cpu" in names


def test_from_dir_missing_directory_returns_empty():
    reg = SkillRegistry.from_dir("/tmp/does_not_exist_sentinel_test_xyz")
    assert len(reg.cards) == 0


def test_from_dir_parses_frontmatter_metadata():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        _write_skill(d, "oom_kill.md", _SKILL_WITH_FRONTMATTER)

        reg = SkillRegistry.from_dir(d)
        card = next(c for c in reg.cards if c.name == "oom_kill")
        assert card.description == "Handle OOMKilled pods"
        assert card.version == "1.1"
        assert "oom" in card.tags


def test_from_dir_derives_name_from_filename_when_no_frontmatter():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        _write_skill(d, "high_cpu.md", _SKILL_NO_FRONTMATTER)

        reg = SkillRegistry.from_dir(d)
        names = [c.name for c in reg.cards]
        assert "high_cpu" in names


def test_get_full_doc_includes_skill_header():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        _write_skill(d, "oom_kill.md", _SKILL_WITH_FRONTMATTER)

        reg = SkillRegistry.from_dir(d)
        doc = reg.get_full_doc("oom_kill")
        assert "oom_kill" in doc
        assert "OOM Kill Runbook" in doc


def test_system_prompt_section_includes_all_card_names():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        _write_skill(d, "oom_kill.md", _SKILL_WITH_FRONTMATTER)
        _write_skill(d, "high_cpu.md", _SKILL_NO_FRONTMATTER)

        reg = SkillRegistry.from_dir(d)
        section = reg.system_prompt_section()
        assert "oom_kill" in section
        assert "high_cpu" in section


def test_system_prompt_section_empty_registry():
    reg = SkillRegistry()
    section = reg.system_prompt_section()
    assert isinstance(section, str)


def test_system_prompt_section_only_filters_by_name_or_tag():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        _write_skill(d, "oom_kill.md", _SKILL_WITH_FRONTMATTER)
        _write_skill(d, "high_cpu.md", _SKILL_NO_FRONTMATTER)

        reg = SkillRegistry.from_dir(d)

        by_name = reg.system_prompt_section(only=["oom_kill"])
        assert "oom_kill" in by_name
        assert "high_cpu" not in by_name

        by_tag = reg.system_prompt_section(only=["pod"])
        assert "oom_kill" in by_tag

        assert reg.system_prompt_section(only=[]) == ""

        all_section = reg.system_prompt_section(only=None)
        assert "oom_kill" in all_section
        assert "high_cpu" in all_section


def test_find_similar_matches_by_tag_overlap():
    from prodagent.skills.registry import SkillCard, SkillContent

    reg = SkillRegistry()
    reg.register(
        SkillContent(
            card=SkillCard(name="a", description="d", tags=["pod", "kubernetes", "restart"]),
            full_doc="x",
        )
    )
    assert reg.find_similar(SkillCard(name="b", description="d", tags=["pod", "kubernetes"])) == "a"
    assert reg.find_similar(SkillCard(name="a", description="d", tags=[])) == "a"
    assert reg.find_similar(SkillCard(name="c", description="d", tags=["pod"])) is None


def test_system_prompt_section_includes_descriptions():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        _write_skill(d, "oom_kill.md", _SKILL_WITH_FRONTMATTER)

        reg = SkillRegistry.from_dir(d)
        section = reg.system_prompt_section()
        assert "Handle OOMKilled pods" in section


def test_len_registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        d = Path(tmpdir)
        _write_skill(d, "a.md", _SKILL_NO_FRONTMATTER)
        _write_skill(d, "b.md", _SKILL_NO_FRONTMATTER)
        _write_skill(d, "c.md", _SKILL_NO_FRONTMATTER)

        reg = SkillRegistry.from_dir(d)
        assert len(reg) == 3
