# Contributing to prodagent

感谢你愿意贡献。这个仓库的门槛不高，但有几条纪律是这个框架成立的原因，读完后你的 PR 会快很多。

## 开发环境

```bash
# 需要 Python ≥ 3.11；uv 管理一切（make 目标会自动装 uv）
make playground        # 起本地 playground（首次自动跑配置向导）

# 或者只装开发环境
uv sync --extra dev
```

开发环境包含两个 LLM adapter SDK 与 playground 依赖（`[openai]` `[anthropic]` `[playground]`），发布的核心包不包含它们——**框架做薄**是刻意的。

## 提交前必须过

```bash
make lint    # ruff check + format --check + mypy (strict)
make test    # 全量测试，离线（conftest 强制 USE_FAKE_LLM=1）
```

CI 在 3.11/3.12/3.13 三个版本上跑同一套。测试离线可跑是一条产品特性，不要引入需要真实 API key 或外部服务才能通过的测试。

## 四条设计纪律

PR 评审会按这四条问：

1. **机制进框架，策略用户注入。** 发现自己在框架里写死一个正则、一个审批矩阵、一个"什么算危险"的判断——停下，把它变成一个用户可注入的卡位（参考 `guardrail/injection/policy.py`、messaging 的 `BEFORE_CONTRACT`/`AFTER_CONTRACT` 开放卡位）。
2. **不重新引入框架级锁。** 互斥语义下放给工具：返回 `ErrorReason.RESOURCE_BUSY`，让上层决定。框架内唯一保留的锁在 stage 原语的 `single_winner` 派发。
3. **所有存储端口皆 async。** 新增端口或给旧端口加方法时，保持 async——网络后端不允许阻塞事件循环。`tests/core/test_port_async_contract.py` 守护这条。
4. **at-least-once + 接收方幂等。** 框架只铸造 crash-stable 的幂等键（`run_id:step_id:attempt`），不承诺 exactly-once，不引入分布式锁或 2PC。

## 约定

- **公共 API 面** = `prodagent.*` 顶层导出（`Agent` / `tool` / `HardBudget` …）。深路径（`prodagent.runtime.…`）可用但可随 CHANGELOG `Changed` 条目演化，不提供兼容别名——1.x 内破坏性变更全部记录在 CHANGELOG。
- **Commit message**：一句话说清"为什么"，中英皆可。参考 `git log`（例如 "The framework's only job here is to mint a crash-stable idempotency key; enforcing idempotency is the tool's responsibility."）。
- **测试写行为不写 mock**：断言可观察的不变量（卡位顺序、崩溃重跑派生相同幂等键、真实计算次数），而不是"这个函数被调了三次"。`tests/runtime/test_messaging_pipeline.py` 是样板。
- **Docstring 当设计文档写**：模块头部解释为什么这样设计、反对什么。`runtime/coordination/messaging/pipeline.py` 的头部是基准线。
- 贡献需要签署 [CLA](CLA.md)；代码以 AGPL-3.0 发布。
