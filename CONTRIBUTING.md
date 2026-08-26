# 贡献指南

> 感谢你考虑为 prodagent 做贡献！无论你是想修一个 bug、加一个功能、改进文档，还是只是提一个问题，你的参与都让这个框架变得更好。
>
> 这份指南帮你快速上手，确保你的贡献能顺利被合并。

---

## 目录

- [行为准则](#行为准则)
- [我能贡献什么？](#我能贡献什么)
- [开发环境设置](#开发环境设置)
- [项目结构速览](#项目结构速览)
- [代码规范](#代码规范)
- [测试规范](#测试规范)
- [文档规范](#文档规范)
- [提交 PR 的流程](#提交-pr-的流程)
- [架构约束（不能违反的）](#架构约束不能违反的)
- [从哪里开始？](#从哪里开始)
- [常见问题](#常见问题)

---

## 行为准则

参与本项目即表示你同意遵守 [Contributor Covenant](https://www.contributor-covenant.org/) 行为准则。简单来说：

- 尊重他人，友善沟通
- 接受不同的观点和经验
- 对事不对人
- 营造包容、友好的社区氛围

违规行为可以通过 GitHub Issue 或私信维护者举报。

---

## 我能贡献什么？

贡献不只是代码。以下都是宝贵的贡献：

### 🐛 报告 Bug

发现了 bug？请提 Issue，包含：

- 复现步骤（最小可复现代码最佳）
- 期望行为 vs 实际行为
- 环境信息（Python 版本、prodagent 版本、操作系统）
- 相关的日志/错误信息

### 💡 提议功能

有新想法？请提 Issue，包含：

- 你想解决什么问题
- 你的提议方案
- 为什么这个功能对 prodagent 有价值
- 可选的实现思路（不要求完整）

### 📝 改进文档

文档永远可以更好。你可以：

- 修正错别字/语法错误
- 补充缺失的说明
- 改进代码示例
- 翻译文档（目前有中文和英文）
- 添加新的教程/示例

### 🔧 贡献代码

- 修复 bug
- 实现新功能
- 重构代码（保持行为不变）
- 优化性能
- 添加测试

### ❓ 回答问题

在 Discussion 或 Issue 里回答其他用户的问题，也是重要的贡献。

---

## 开发环境设置

### 前置要求

- Python 3.11 或更高版本
- [uv](https://docs.astral.sh/uv/)（推荐）或 pip
- Git

### 步骤

```bash
# 1. Fork 并克隆仓库
git clone https://github.com/<your-username>/prodagent.git
cd prodagent

# 2. 创建虚拟环境并安装开发依赖
uv sync --extra dev          # 用 uv（推荐）
# 或者：
pip install -e ".[dev]"     # 用 pip

# 3. 验证安装
python -c "import prodagent; print(prodagent.__version__)"

# 4. 跑测试（确保环境正常）
pytest tests/ -x -q
```

### 可选：安装额外依赖

```bash
# 模型 provider
uv sync --extra openai --extra anthropic

# 生产后端
uv sync --extra postgres --extra redis --extra neo4j

# 可视化 playground
uv sync --extra playground

# 全部安装
uv sync --extra all
```

### 用 FakeLLM 离线开发

prodagent 的核心测试全部离线运行，不需要 API key：

```bash
export USE_FAKE_LLM=1
pytest tests/ -x -q
```

这是推荐的开发方式——快速、确定、零成本。

---

## 项目结构速览

```
prodagent/
├── src/prodagent/          # 核心源码
│   ├── base/               # 基础：配置、错误、事件日志、会话
│   ├── ports/              # 14 个 Protocol 端口
│   ├── kernel/             # 纯内核：循环、步骤、预算、总线
│   ├── runtime/            # 运行时：Agent 装配、工厂
│   ├── llm/                # 模型适配：OpenAI/Anthropic/Fake
│   ├── tooling/            # 工具系统：装饰器、调度、注册
│   ├── plan/               # 规划：DAG、PlanExecutor、Workflow
│   ├── coordination/       # 多 Agent：5种协作原语 + 消息平面
│   ├── cognition/          # 认知：上下文压缩、四通道记忆
│   ├── hooks/              # 横切：审批、权限、可观测、审计
│   ├── skills/             # 技能：runbook 蒸馏与召回
│   ├── mcp/                # MCP 协议桥接
│   ├── backends/           # 端口实现：file/memory/postgres/redis/neo4j
│   └── playground/         # 可视化（叶子节点）
├── tests/                  # 测试（1,182 个，全离线）
├── examples/               # 9 个端到端示例
├── docs/                   # 文档（mkdocs）
├── pyproject.toml          # 项目配置
├── Makefile                # 常用命令
└── CLA.md                  # 贡献者许可协议
```

### 阅读顺序建议

如果你是第一次接触代码库，建议按这个顺序读：

1. `base/types.py` — 基础类型（Message, RunState, ExecutionMode）
2. `kernel/types.py` — 核心词汇（ToolCall, LLMResponse, ToolResult, AgentEvent）
3. `kernel/state.py` — AgentRun（运行状态对象）
4. `kernel/budget.py` — 四轴预算
5. `kernel/bus.py` — 三协议总线
6. `kernel/step.py` — Step（agency 原子）
7. `kernel/loop.py` — ReactiveLoop（循环策略）
8. `runtime/agent.py` — Agent（公共 API）
9. `runtime/factory.py` — 装配工厂
10. 然后按兴趣深入：`tooling/`、`plan/`、`coordination/`、`cognition/`...

---

## 代码规范

### 格式化

我们用 [ruff](https://docs.astral.sh/ruff/) 做格式化和 lint：

```bash
# 格式化
ruff format src/ tests/

# lint
ruff check src/ tests/

# 自动修复可修复的问题
ruff check --fix src/ tests/
```

CI 会检查格式化和 lint，未通过的 PR 不会被合并。

### 类型检查

我们用 [mypy](https://mypy-lang.org/) 做严格类型检查：

```bash
mypy src/prodagent/
```

**所有新代码必须通过 mypy strict 模式。** 这意味着：

- 所有函数必须有类型注解
- 不允许 `Any`（除非有充分理由并加注释）
- 不允许未类型化的变量

### 代码风格

- **行长度**：100 字符（ruff 配置）
- **命名**：函数/变量用 snake_case，类用 PascalCase，常量用 UPPER_SNAKE_CASE
- **导入顺序**：标准库 → 第三方 → 第一方（prodagent），ruff isort 自动处理
- **docstring**：公共 API 必须有 docstring，说明"做什么"和"为什么"
- **注释**：解释"为什么"，不是"做什么"。代码本身应该能说明"做什么"

### 注释的黄金法则

> 好的注释解释"为什么这么做"，而不是"代码在做什么"。

```python
# ❌ 坏注释：解释代码在做什么
# 增加计数器
count += 1

# ✅ 好注释：解释为什么
# 用单调时钟而非 wall-clock，因为 NTP 校准可能导致时间回退
elapsed = time.monotonic() - start
```

prodagent 的代码注释质量很高，照着这个风格写就对了。

---

## 测试规范

### 测试哲学

prodagent 的测试有三个核心原则：

1. **全离线**——测试不调用真实 API，不需要网络，不需要 API key
2. **确定性**——同一个测试跑 100 次，结果完全一样
3. **快速**——全量测试在 30 秒内跑完

### 用 FakeLLM 写测试

所有涉及 LLM 调用的测试都用 `FakeLLMAdapter`：

```python
from prodagent.llm.fake import FakeLLMAdapter, script
from prodagent.kernel.types import LLMResponse, StopReason, ToolCall

# 方式 1：直接构造
llm = FakeLLMAdapter(script=[
    LLMResponse(
        content="",
        tool_calls=[ToolCall(name="search", params={"query": "weather"})],
        stop_reason=StopReason.TOOL_USE,
    ),
    LLMResponse(content="巴黎今天晴天。", stop_reason=StopReason.END_TURN),
])

# 方式 2：用 script DSL
llm = script("""
[
  {"tool_calls": [{"name": "search", "params": {"query": "weather"}}]},
  {"content": "巴黎今天晴天。"}
]
""")
```

### 测试结构

```python
# tests/your_module/test_your_feature.py
import pytest

class TestYourFeature:
    def test_basic_case(self):
        """测试基本行为。"""
        # Arrange
        ...
        # Act
        ...
        # Assert
        ...

    @pytest.mark.asyncio
    async def test_async_case(self):
        """测试异步行为。"""
        ...

    def test_edge_case(self):
        """测试边界条件。"""
        ...
```

### 运行测试

```bash
# 全量测试
pytest tests/

# 单个测试文件
pytest tests/kernel/test_step.py

# 单个测试函数
pytest tests/kernel/test_step.py::TestStep::test_basic_case

# 失败时停止
pytest tests/ -x

# 显示 print 输出
pytest tests/ -s

# 覆盖率
pytest tests/ --cov=src/prodagent --cov-report=term-missing
```

### 测试要求

- **新功能必须有测试**——没有测试的 PR 不会被合并
- **bug 修复必须有回归测试**——先写一个能复现 bug 的测试（确认失败），再修复（确认通过）
- **测试必须离线**——不要在测试里调用真实 API
- **测试必须确定**——不要依赖时间、随机数、网络等不确定因素

---

## 文档规范

### 文档在哪里

- `README.md` — 项目门面（中文）
- `README.en.md` — 项目门面（英文）
- `docs` — mkdocs 文档站
  - `docs/index.md` — 文档首页
  - `docs/start.md` — 5 分钟上手
  - `docs/tour` — 七站之旅
  - `docs/topics` — 生产问题域专题
  - `docs/architecture.md` — 架构全景
  - `docs/design-philosophy.md` — 设计哲学
  - `docs/mental-model.md` — 心智模型
  - `docs/decisions.md` — 设计取舍
  - `docs/glossary.md` — 术语表
  - `docs/examples.md` — 示例地图
  - `docs/reference.md` — API 参考

### 文档风格

- **先讲"为什么"，再讲"怎么做"**——读者需要知道为什么这个机制存在，才能理解怎么用
- **用代码示例说话**——一个好的示例胜过千言万语
- **保持简洁**——不要用 100 字说 10 字能说清的事
- **中英双语**——核心文档有中文和英文版本，新增文档请至少写中文
- **代码示例必须可运行**——读者复制粘贴就能跑

### 本地预览文档

```bash
# 安装文档依赖
uv sync --extra docs

# 启动本地文档服务器
mkdocs serve
# 访问 http://127.0.0.1:8000
```

---

## 提交 PR 的流程

### 1. 创建分支

```bash
git checkout -b feature/your-feature-name
# 或
git checkout -b fix/your-bug-fix
```

分支命名建议：
- `feature/xxx` — 新功能
- `fix/xxx` — bug 修复
- `docs/xxx` — 文档改进
- `refactor/xxx` — 代码重构
- `test/xxx` — 测试改进

### 2. 提交代码

```bash
git add .
git commit -m "feat: add xxx feature"
```

提交信息格式（Conventional Commits）：

- `feat: xxx` — 新功能
- `fix: xxx` — bug 修复
- `docs: xxx` — 文档
- `refactor: xxx` — 重构（不改变行为）
- `perf: xxx` — 性能优化
- `test: xxx` — 测试
- `chore: xxx` — 构建/工具链

### 3. 确保本地检查通过

```bash
# 格式化
ruff format src/ tests/

# lint
ruff check src/ tests/

# 类型检查
mypy src/prodagent/

# 测试
pytest tests/ -x -q
```

### 4. 推送并创建 PR

```bash
git push origin feature/your-feature-name
```

然后在 GitHub 上创建 Pull Request。

### 5. PR 描述模板

请在 PR 描述中包含：

```markdown
## 变更内容
<!-- 简要描述这个 PR 做了什么 -->

## 为什么需要这个变更
<!-- 解决了什么问题？关联了哪个 Issue？ -->
Closes #xxx

## 测试
<!-- 如何测试这个变更？新增了哪些测试？ -->
- [ ] 单元测试
- [ ] 集成测试
- [ ] 手动测试（描述步骤）

## 检查清单
- [ ] 代码通过 ruff format 和 ruff check
- [ ] 代码通过 mypy strict
- [ ] 所有测试通过
- [ ] 新功能有测试
- [ ] 文档已更新（如需要）
- [ ] 没有违反架构约束（见下文）

## 其他说明
<!-- 任何需要维护者注意的事项 -->
```

### 6. 代码审查

- 维护者会在 1-3 个工作日内审查 PR
- 可能会要求修改——请积极回应，修改后推送（同一分支自动更新 PR）
- 审查通过后，维护者会合并 PR

### 7. CLA 签署

首次贡献需要签署 [CLA（贡献者许可协议）](CLA.md)。CI 会自动检查，未签署的 PR 不会被合并。

---

## 架构约束（不能违反的）

这些是 prodagent 的核心架构原则，CI 会自动检查。违反这些约束的 PR 不会被合并。

### 约束 1：kernel 不依赖任何 capability 包

`kernel/` 只能 `import`：
- `base/`
- `ports/`
- 标准库

不能 `import`：
- `runtime/`
- `llm/`
- `tooling/`
- `plan/`
- `coordination/`
- `cognition/`
- `hooks/`
- `skills/`
- `mcp/`
- `backends/`
- `playground/`

**检查方式：** `tests/base/test_layering_contract.py` + `tests/base/test_kernel_purity.py`

### 约束 2：playground 是叶子节点

没有任何 prodagent 内部模块可以 `import playground`。

**检查方式：** `pyproject.toml` 中的 `import-linter` 配置

### 约束 3：ports 只能定义 Protocol，不能有实现

`ports/` 下的文件只能定义 Protocol（抽象接口），不能有具体实现。实现在 `backends/` 下。

### 约束 4：核心依赖只有 4 个

`pyproject.toml` 的 `dependencies` 只能有：
- `anyio`
- `httpx`
- `pydantic`
- `typing-extensions`

其他所有依赖都必须是 optional（在 `optional-dependencies` 中）。

### 约束 5：测试必须全离线

`tests` 下的测试不能调用真实 API，不需要网络，不需要 API key。

**检查方式：** CI 环境不设置任何 API key，如果测试需要 API key 会失败。

### 约束 6：公共 API 必须有类型注解

`prodagent/__init__.py` 中导出的所有符号必须有完整的类型注解，通过 mypy strict。

---

## 从哪里开始？

如果你是第一次贡献，这里有一些建议：

### Good First Issues

在 GitHub Issues 中搜索标签 `good first issue`——这些是专门为新贡献者准备的任务，通常：

- 范围明确
- 不需要深入理解整个代码库
- 有明确的验收标准
- 维护者会提供额外指导

### 文档改进

文档改进是最好的入门方式：

- 修正错别字
- 改进代码示例
- 补充缺失的说明
- 添加新的教程

你不需要深入理解代码就能改进文档，而且文档改进对所有用户都有价值。

### 测试改进

- 为未覆盖的代码添加测试
- 改进现有测试的断言
- 添加边界条件测试

写测试是理解代码的最好方式——你需要理解代码的行为才能写出正确的测试。

### Bug 修复

搜索标签 `bug` 的 Issue，挑一个你能复现的：

1. 先写一个能复现 bug 的测试（确认失败）
2. 修复 bug（确认测试通过）
3. 提交 PR

### 示例项目

`examples` 下有 9 个示例。你可以：

- 改进现有示例
- 添加新的示例（展示某个机制的用法）
- 为示例添加更详细的 README

---

## 常见问题

### Q: 我需要签署 CLA 吗？

A: 是的，首次贡献需要签署 CLA。CI 会自动引导你完成。这是为了保护项目和用户，确保贡献的代码可以合法地分发。

### Q: 我的 PR 多久会被审查？

A: 维护者通常在 1-3 个工作日内审查。如果超过一周没有回应，可以在 PR 里 @维护者 提醒。

### Q: 我可以提多大的 PR？

A: 建议保持 PR 小而聚焦——一个 PR 做一件事。大 PR 审查困难、合并周期长。如果你的变更很大，可以拆成多个小 PR 逐步提交。

### Q: 我不确定我的想法是否适合 prodagent，怎么办？

A: 先提 Issue 讨论！在写代码之前，先在 Issue 里描述你的想法，维护者会给反馈。这样可以避免你花了很多时间写代码，最后发现方向不对。

### Q: 我可以加新的依赖吗？

A: 核心依赖（`dependencies`）只有 4 个，不建议增加。如果你的功能需要新依赖，可以把它做成 optional（`optional-dependencies`），用户按需安装。

### Q: 测试失败了但不是我的代码导致的，怎么办？

A: 先确认是在 main 分支上也失败（`git checkout main && pytest`）。如果 main 上也失败，说明是已有问题，可以提 Issue 报告。如果 main 上通过但你的分支失败，说明是你的变更导致的，需要修复。

### Q: 如何联系维护者？

A: 
- GitHub Issue / Discussion（推荐，公开透明）
- PR 里 @维护者
- 邮箱（见 GitHub 个人资料）

---

## 感谢

每一个贡献——无论大小——都让 prodagent 变得更好。感谢你花时间阅读这份指南，期待你的贡献！

> **如果你觉得这个框架有价值，请点个 Star ⭐。你的每一个 Star 都是对"好的架构应该被看见"的投票。**
