# 代码侦探

> 示例 #6 —— MCP 工具 + REACTIVE 多轮调试 + 学习合成的自主修 bug agent。

agent 拿到「`tests/test_user.py::test_login` 失败」的 bug report,在 fixture
repo 里自主调试:

1. **加载 runbook** —— `get_skill("debug-workflow")` 学修 bug 流程。
2. **读失败测试** —— `read_file("tests/test_user.py")` 看断言什么、期望什么。
3. **grep 定位** —— `grep("def login")` 找到 login 函数在 auth.py:22。
4. **读源码** —— `read_file("auth.py")` 看到 `login` 用 `hash(password)` 不加 salt。
5. **提 patch(错)** —— `apply_patch` 写第一版修复,但仍然用 `hash()` 漏 salt。
6. **跑测试(失败)** —— `run_tests` 返回 `passed=false`,traceback 显示 assert 失败。
7. **读错误信息换思路** —— LLM 看 traceback,读 `password.py` 发现 `hash_with_salt`。
8. **提 patch(对)** —— `apply_patch` 写第二版,`from password import hash_with_salt`。
9. **跑测试(通过)** —— `run_tests` 返回 `passed=true`。
10. **总结** —— 输出根因 + 修复 + 验证。
11. **学习合成** —— SESSION_END 触发 `SkillSynthesizer` 合成 `test-login-failure.md`
    runbook,持久化到 skills/ 目录,下次类似 bug 直接走 runbook。

## 本示例展示什么

- **`mcp=[MCPServerConfig(...)]`** —— spawn `code_detective.mcp_server` 子进程,
  stdio JSON-RPC,桥接 4 个工具为 `mcp__code_detective__<tool>`。MCP server 是真正的
  独立进程,不是 mock —— 框架通过 stdio 连接它,发现工具,通过 JSON-RPC 调用。
- **REACTIVE 多轮调试** —— 每 turn LLM 发一个 tool_call,看结果后决定下一步。
  不是一次性生成 plan,而是根据测试结果动态调整(读错误信息 → 换思路)。
- **`LearningHooks`** —— SESSION_END 时 `ExperienceRecord.from_run(run)` 记录经验,
  `SkillSynthesizer.maybe_synthesize(rec)` 用 aux LLM 合成 skill,命中就
  `registry.register()` 持久化到 `skills/<name>.md`。
- **`SkillRegistry` 渐进披露** —— `debug-workflow.md` 常驻 system prompt 目录,
  LLM 调 `get_skill("debug-workflow")` 加载完整 runbook。

## 为什么需要这个

生产代码 agent 不只是「写代码」—— 要能:

- **多轮调试**:第一次 patch 没修好,要读错误信息换思路,不是死磕同一个方案。
- **MCP 外部工具**:文件系统、测试运行器、版本控制都是外部工具,不该硬编码进 agent。
  MCP 让工具通过标准协议桥接,agent 代码不变,工具可换。
- **学习进化**:修过的 bug 应该沉淀成 runbook,下次类似 bug 直接走 runbook,
  不重新调试。LearningHooks 自动合成 + 持久化。

| 单次 patch | Code Detective |
|------------|----------------|
| 提一次 patch 就完事 | 多轮调试,失败换思路 |
| 工具硬编码进 agent | MCP 桥接,工具可换 |
| 修过的 bug 不留痕 | 自动合成 runbook,下次直接用 |
