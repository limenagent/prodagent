"""skills —— 技能是“打包好的专长”，也是可替换策略（第 28、29 课）。

工具是一个原子动作；技能是一组相关工具 + 一段专门的操作指引（instructions）
+ 可选资源，打包成一个可被按需选用的专长包。它和记忆、上下文一样不进内核：

- 注册：声明技能名、说明、操作指引、要用的工具名；
- 选用：resolve 按名字精确取，match 按当前任务描述挑最相关的；
- 落地：把技能的 instructions 拼进 system，并把工具收敛到技能声明的范围。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Skill:
    name: str
    description: str
    instructions: str = ""
    tools: list[str] = field(default_factory=list)   # 该技能涉及的工具名


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", text.lower()))


class SkillRegistry:
    def __init__(self):
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill) -> "SkillRegistry":
        self._skills[skill.name] = skill
        return self

    def resolve(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def match(self, task: str) -> Skill | None:
        """按任务描述与技能说明的词重叠，挑最相关的一个（教学版相关性）。"""
        q = _tokens(task)
        best, best_score = None, 0
        for skill in self._skills.values():
            score = len(q & _tokens(skill.description + " " + skill.name))
            if score > best_score:
                best, best_score = skill, score
        return best

    def apply_to_system(self, skill: Skill, system: str = "") -> str:
        """把技能指引拼进系统提示。"""
        if not skill:
            return system
        return (system + "\n\n" if system else "") + f"使用技能「{skill.name}」：\n{skill.instructions}"

    # —— 从目录加载：每个子目录一份 SKILL.md，就是一个可渐进披露的技能 ——
    @staticmethod
    def parse_skill_md(text: str) -> Skill:
        """解析一份 SKILL.md：开头三横线之间写 name/description，其后正文即指引。

        故意只认最简单的 frontmatter，不引入 YAML 依赖，教学上一眼能看懂。
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
                instructions_text = "\n".join(lines[end + 1:]).strip()
        return Skill(name=meta.get("name", "未命名"),
                     description=meta.get("description", ""),
                     instructions=instructions_text)

    def load_dir(self, root: str) -> list[Skill]:
        """加载 root 下每个含 SKILL.md 的子目录；root 自身若是单个技能也支持。"""
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
