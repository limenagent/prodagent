# Security Policy

## 报告漏洞

请勿通过公开 issue 报告安全漏洞。使用 GitHub 的
[私有漏洞报告](https://github.com/limenagent/prodagent/security/advisories/new)，
或联系维护者。我们会在 72 小时内确认收到。

请包含：影响的模块/文件、复现步骤、你认为的影响面。若你已在真实系统上验证，
请注明是否可以公开致谢。

## 设计立场（读这个能省你一半的报告时间）

prodagent 的安全模型是**机制内置、策略由应用注入**：

- 注入检测管道（`guardrail/injection/`）默认**零配置全放行**——什么算注入、
  什么算敏感内容是应用策略（`InjectionPolicy`），框架不内置任何正则库。
  这不是漏洞，是边界声明：框架不知道你的威胁模型。
- messaging 平面的安全门（`GateInterceptor`）在未注册 checker 时是 no-op。
- "默认配置下无检测"类的报告会被标记为 works-as-designed；但**机制本身的
  绕过**（例如绕过卡位顺序、伪造 `Crossing` 身份、死信边界泄漏）是我们
  最重视的报告类型。

## 支持范围

| 版本 | 支持状态 |
|---|---|
| main | ✅ |
| < 1.0 | ❌ |
