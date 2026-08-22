# 技能

同类任务处理第三次，轮数能不能不减？人排过一次雷会写下 runbook，
下次照着做。`skills/` 是 runbook 系统，加上让 Agent 自己沉淀 runbook
的闭环。

## 读：技能是文件，不是数据库

```python
from prodagent.skills.registry import SkillRegistry

skills = SkillRegistry.from_dir("skills/")   # skills/registry.py:142
agent = Agent("ops", ..., config=AgentConfig(name="ops", skills=skills))
```

一个技能就是一个 markdown 文件（frontmatter 是元数据：名字、描述、
标签；正文是给模型读的操作手册）。注册表把**全部技能的名字和一句话
描述**注入系统提示，模型需要时调 `get_skill(name)` 工具
（`skills/registry.py:229` 提供它的 schema）把全文拉进上下文。

为什么是文件不是数据库行？因为技能的**作者是人**（或要被人 review
的模型）：markdown 可以 diff、可以 code review、可以 git revert。
看一眼 aiops 示例的 `skills/` 目录——`service-alert-triage-and-rollback.md`
就是一条真实的事故处置手册。

按需加载（而不是全量注入）解决的正是[压缩](compression.md)的反面：
知识不占上下文，直到相关的那一刻。

## 写：经验蒸馏闭环

```python
# 成功的 run → ExperienceRecord（ports/experience.py:44）
#   → SkillSynthesizer（skills/skill_synthesizer.py:246）问 aux LLM：
#     "这条轨迹值得沉淀成 runbook 吗？"
#   → 值得 → patch 出 skills/<name>.md（或修订已有技能）
```

`LearningHooks`（`hooks/bundles/learning.py:25`）挂在 SESSION_END 上把
这条链异步跑起来：run 成功且标签命中时，`maybe_synthesize` 产出候选
技能文件——**先落盘成文件，人 review 后生效**。蒸馏不直接改行为：
机器提议、人类批准，和[审批](approval.md)是同一哲学。

轨迹的原始记录走 `ExperienceStore` 端口（JSONL 追加，file 默认）——
即使不蒸馏，这也是一份“系统做过什么”的积累。

## 闭环的另一半：为什么只学成功

失败轨迹也记录（`ExperienceRecord.outcome=FAILURE`），但不进蒸馏——
从失败里归纳 runbook 的误报率太高，一条错误的手册比没有手册更危险。
失败的价值在复盘时由**人**提炼；机器只从可复现的成功里学习。

## 取舍

**为什么不是 few-shot 示例库 / 向量检索的老经验？** 因为示例和经验的
问题是**没有作者**：没人对它负责、没法 review、错了没法 revert。
技能是文件这件事不是实现偷懒，是治理模型——知识变更走代码变更的
流程（diff/review/revert），这对“指导生产操作的文本”是唯一诚实的
门槛。

