"""skills — a skill is "packaged expertise", also a replaceable strategy.

A tool is one atomic action; a skill bundles a set of related tools plus a piece
of dedicated operating instructions and optional resources into an expertise
package selected on demand. Like memory and context, it stays out of the kernel:

- register: declare the skill's name, description, instructions, and tool names;
- select: resolve fetches by exact name, match picks the most relevant one for
  the current task description;
- land: splice the skill's instructions into the system prompt and narrow the
  tools to the scope the skill declares.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Skill:
    name: str
    description: str
    instructions: str = ""
    tools: list[str] = field(default_factory=list)  # tool names this skill involves


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", text.lower()))


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> SkillRegistry:
        self._skills[skill.name] = skill
        return self

    def resolve(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def match(self, task: str) -> Skill | None:
        """Pick the most relevant skill by word overlap with the task (teaching relevance)."""
        q = _tokens(task)
        best, best_score = None, 0
        for skill in self._skills.values():
            score = len(q & _tokens(skill.description + " " + skill.name))
            if score > best_score:
                best, best_score = skill, score
        return best

    def apply_to_system(self, skill: Skill, system: str = "") -> str:
        """Splice the skill's instructions into the system prompt."""
        if not skill:
            return system
        prefix = system + "\n\n" if system else ""
        return prefix + f'Use the skill "{skill.name}":\n{skill.instructions}'

    # ---- Load from a directory: each subdirectory with a SKILL.md is one
    # progressively-disclosable skill. ----
    @staticmethod
    def parse_skill_md(text: str) -> Skill:
        """Parse one SKILL.md: name/description between the leading '---' fences, body after.

        Deliberately understands only the simplest frontmatter and pulls in no
        YAML dependency, so it stays readable at a glance for teaching.
        """
        lines = text.strip().splitlines()
        meta: dict[str, str] = {}
        instructions_text = text.strip()
        if lines and lines[0].strip() == "---":
            end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
            if end:
                for line in lines[1:end]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
                instructions_text = "\n".join(lines[end + 1 :]).strip()
        return Skill(
            name=meta.get("name", "unnamed"),
            description=meta.get("description", ""),
            instructions=instructions_text,
        )

    def load_dir(self, root: str) -> list[Skill]:
        """Load every subdirectory under root that contains SKILL.md; root itself may be one skill."""
        import os

        loaded: list[Skill] = []
        direct = os.path.join(root, "SKILL.md")
        candidates = []
        if os.path.isfile(direct):
            candidates.append(direct)
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                p = os.path.join(root, name, "SKILL.md")
                if os.path.isfile(p):
                    candidates.append(p)
        for path in candidates:
            with open(path, encoding="utf-8") as f:
                skill = self.parse_skill_md(f.read())
            self.register(skill)
            loaded.append(skill)
        return loaded
