"""Skills progressive disclosure — lazy domain knowledge injection.

The whole catalogue rides in the system prompt as one line per skill; a
full document costs pages and is fetched only when the model actually calls
``get_skill`` — the agent pays for depth it uses, not depth it might."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prodagent.base.determinism import now_wall
from prodagent.base.io import safe_filename_component
from prodagent.kernel.types import GET_SKILL_TOOL_NAME

logger = logging.getLogger(__name__)


def _is_disk_safe(name: str) -> bool:
    """True when ``skills_dir / f"{name}.md"`` cannot escape the skills dir."""
    try:
        safe_filename_component(name)
        return True
    except ValueError:
        return False


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class SkillCard:
    """Minimal metadata that is always present in the context window."""

    name: str
    description: str
    version: str = "1.0"
    tags: list[str] = field(default_factory=list)

    def to_context_line(self) -> str:
        return f"• {self.name}: {self.description}"


@dataclass
class SkillContent:
    """Full skill documentation, loaded on demand."""

    card: SkillCard
    full_doc: str
    contradictions: list[dict[str, str]] = field(default_factory=list)

    def to_injection_block(self) -> str:
        lines = [f"[SKILL: {self.card.name} v{self.card.version}]", self.full_doc.strip()]
        return "\n".join(lines)


def _parse_skill_file(path: Path) -> SkillContent:
    """Parse a Markdown skill file with optional YAML front-matter."""
    raw = path.read_text(encoding="utf-8")

    meta: dict[str, Any] = {}
    body = raw

    m = _FRONTMATTER_RE.match(raw)
    if m:
        body = raw[m.end() :]
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "Loading skill frontmatter requires pyyaml: pip install pyyaml"
            ) from exc

        try:
            loaded = yaml.safe_load(m.group(1))
            if isinstance(loaded, dict):
                meta = loaded
        except yaml.YAMLError:
            pass

    stem = path.stem
    name = str(meta.get("name", stem))
    version = str(meta.get("version", "1.0"))
    tags_raw = meta.get("tags", [])
    tags = tags_raw if isinstance(tags_raw, list) else [tags_raw]

    description = str(meta.get("description", ""))
    if not description:
        # No front-matter description: fall back to the first heading line —
        # the catalogue always needs a one-liner per skill.
        for line in body.splitlines():
            stripped = line.lstrip("#").strip()
            if stripped:
                description = stripped[:120]
                break

    card = SkillCard(name=name, description=description, version=version, tags=tags)
    return SkillContent(card=card, full_doc=body.strip())


def _to_markdown(content: SkillContent) -> str:
    """Render *content* as a Markdown file with YAML front-matter — the
    on-disk format shared by ``.md`` skill files and ``.history/`` snapshots."""
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("Persisting skills requires pyyaml: pip install pyyaml") from exc

    front_matter = yaml.safe_dump(
        {
            "name": content.card.name,
            "description": content.card.description,
            "version": content.card.version,
            "tags": content.card.tags,
        },
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()
    return f"---\n{front_matter}\n---\n\n{content.full_doc}"


class SkillRegistry:
    """Progressive-disclosure skill registry."""

    def __init__(self, skills_dir: Path | None = None) -> None:
        self._skills: dict[str, SkillContent] = {}  # name → content
        self._skills_dir = skills_dir  # set to persist synthesised skills
        self._lock = threading.RLock()

    @property
    def root(self) -> Path | None:
        """On-disk directory backing this registry, or ``None`` if in-memory."""
        return self._skills_dir

    def register(self, content: SkillContent) -> Path | None:
        """Register a SkillContent in memory, and persist to disk if skills_dir is set.

        If this overwrites a previously-registered version of the same skill,
        the version being replaced is archived to ``.history/`` first so a bad
        auto-synthesis can be undone with :meth:`rollback`.
        """
        with self._lock:
            previous = self._skills.get(content.card.name)
            self._skills[content.card.name] = content
        logger.debug("Skill registered: %s", content.card.name)
        if self._skills_dir is not None:
            if previous is not None:
                self._archive(previous)
            return self._persist(content)
        return None

    def _history_dir(self, name: str) -> Path | None:
        if self._skills_dir is None or not _is_disk_safe(name):
            return None
        return self._skills_dir / ".history" / name

    def _archive(self, content: SkillContent) -> None:
        """Snapshot *content* before it's overwritten by a newer version."""
        history_dir = self._history_dir(content.card.name)
        if history_dir is None:
            return
        try:
            history_dir.mkdir(parents=True, exist_ok=True)
            path = history_dir / f"{int(now_wall())}_v{content.card.version}.md"
            path.write_text(_to_markdown(content), encoding="utf-8")
            logger.info("Skill archived before overwrite: %s", path)
        except OSError:
            logger.exception(
                "SkillRegistry: failed to archive skill %r before overwrite", content.card.name
            )

    def rollback(self, name: str, to_version: str) -> bool:
        """Restore *name* to *to_version* from its version history.

        Re-registers the archived snapshot, which itself archives the version
        being replaced — a rollback is never destructive. Returns ``False``
        if no archived snapshot for *to_version* exists (including when this
        registry has no ``skills_dir``, since history is disk-only).
        """
        history_dir = self._history_dir(name)
        if history_dir is None or not history_dir.exists():
            return False  # in-memory registry (or never archived) — nothing to restore
        candidates = sorted(history_dir.glob(f"*_v{to_version}.md"))
        if not candidates:
            return False  # that version was never archived
        content = _parse_skill_file(candidates[-1])  # latest snapshot of that version
        self.register(
            content
        )  # re-register archives the current one — rollback is never destructive
        logger.info("Skill %r rolled back to v%s from %s", name, to_version, candidates[-1])
        return True

    def _persist(self, content: SkillContent) -> Path | None:
        """Write a synthesised skill to disk as a Markdown file with YAML front-matter."""
        if self._skills_dir is None:
            return None
        if not _is_disk_safe(content.card.name):
            # Defence in depth: the synthesizer sanitizes names, but the
            # registry must not trust its input — a name carrying a path
            # separator would write outside the skills directory.
            logger.error(
                "SkillRegistry: refusing to persist skill %r — unsafe name", content.card.name
            )
            return None
        skills_dir = self._skills_dir
        try:
            skills_dir.mkdir(parents=True, exist_ok=True)
            path = skills_dir / f"{content.card.name}.md"
            path.write_text(_to_markdown(content), encoding="utf-8")
            logger.info("Skill persisted to disk: %s", path)
            return path
        except OSError:
            logger.exception("SkillRegistry: failed to persist skill %r", content.card.name)
        return None

    @classmethod
    def from_dir(cls, skills_dir: str | Path, *, glob: str = "*.md") -> SkillRegistry:
        """Build a SkillRegistry from a directory of Markdown skill files."""
        dir_path = Path(skills_dir)
        registry = cls(skills_dir=dir_path)
        if not dir_path.exists():
            logger.warning("Skills directory not found: %s", dir_path)
            return registry

        count = 0
        for path in sorted(dir_path.glob(glob)):
            try:
                content = _parse_skill_file(path)
                with registry._lock:
                    registry._skills[content.card.name] = content
                count += 1
            except Exception as exc:
                logger.warning("Failed to parse skill file %s: %s", path, exc)

        logger.info("SkillRegistry: loaded %d skill cards from %s", count, dir_path)
        return registry

    @property
    def cards(self) -> list[SkillCard]:
        """All registered skill cards (metadata only)."""
        with self._lock:
            return [s.card for s in self._skills.values()]

    def find_similar(self, card: SkillCard, *, min_tag_overlap: int = 2) -> str | None:
        """Return the name of an existing skill that likely covers *card*, or None."""
        with self._lock:
            if card.name in self._skills:
                return card.name
            return self.find_by_tags(card.tags, min_tag_overlap=min_tag_overlap)

    def find_by_tags(
        self,
        tags: list[str] | set[str] | tuple[str, ...],
        *,
        min_tag_overlap: int = 2,
    ) -> str | None:
        """Return the name of an existing skill whose tags overlap *tags*, or None."""
        incoming = set(tags)
        if not incoming:
            return None
        with self._lock:
            best: tuple[int, str] | None = None
            for existing in self._skills.values():
                overlap = len(incoming & set(existing.card.tags))
                if overlap >= min_tag_overlap and (best is None or overlap > best[0]):
                    best = (overlap, existing.card.name)
        return best[1] if best else None

    def get(self, name: str) -> SkillContent | None:
        """Return full SkillContent by name, or None if not found."""
        with self._lock:
            return self._skills.get(name)

    def __len__(self) -> int:
        with self._lock:
            return len(self._skills)

    def names(self) -> list[str]:
        with self._lock:
            return list(self._skills.keys())

    def get_full_doc(self, name: str) -> str:
        """Return the injection block for *name*, or an error string."""
        content = self.get(name)
        if content is None:
            with self._lock:
                available = ", ".join(self._skills.keys())
            return f"Skill {name!r} not found. Available skills: {available or '(none registered)'}"
        return content.to_injection_block()

    def system_prompt_section(self, *, only: list[str] | None = None) -> str:
        """Return a compact skill directory for inclusion in the system prompt."""
        cards = self.cards
        if only is not None:
            allow = set(only)
            cards = [c for c in cards if c.name in allow or (allow & set(c.tags))]
        if not cards:
            return ""
        lines = ["## Available Skills (call get_skill(name) for full documentation)"]
        for card in cards:
            lines.append(card.to_context_line())
        return "\n".join(lines)

    def as_tool_schema(self) -> dict[str, Any]:
        """Return the Anthropic-compatible tool schema dict for get_skill."""
        return {
            "name": GET_SKILL_TOOL_NAME,
            "description": (
                "Retrieve the full documentation for a named skill. "
                "Call this before using any tools listed under a skill you are "
                "unfamiliar with.  Use the skill list in the system prompt to "
                "discover available skill names."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill name as shown in the system prompt skill list.",
                    }
                },
                "required": ["name"],
            },
        }

    def path_for(self, name: str) -> Path | None:
        """Return the on-disk path where *name* is persisted, or ``None``."""
        with self._lock:
            if self._skills_dir is None or name not in self._skills:
                return None
            return self._skills_dir / f"{name}.md"
