# ② 契约 ports

框架里最重要的一段代码可能是这个——模型客户端的全部契约：

```python
# src/prodagent/ports/llm.py:20
@runtime_checkable
class LLMClient(Protocol):
    """Structural interface every provider adapter satisfies (duck-typed)."""

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse: ...
```

一个方法。Anthropic 适配器、OpenAI 兼容适配器、离线 FakeLLM、你 mock
的任何东西，都只承诺这一件事。`ports/` 里全是这样的文件：**Protocol
先于实现**——不是因为教科书这么说，而是因为有两件事必须先钉死。

## 十四份契约

| 契约 | 管什么 | 默认实现 | 生产替换 |
|---|---|---|---|
| `LLMClient` | 调模型 | env 解析（fake/openai 兼容/anthropic） | 你的路由/代理 |
| `Tool` / `LeafExecutor` | 工具与执行器（循环契约） | `FunctionTool` / `ReactiveLoop` / `PlanExecutor` | — |
| `CheckpointStore` | 断点续跑 | file（`.prodagent/runs`） | postgres |
| `SessionStore` | 多轮会话根 | 内存（bare）/ file（production） | postgres |
| `EventLog` | 计划事件日志 | file | postgres |
| `SpanExporter` | 追踪导出 | file JSONL / 日志 | postgres |
| `DocumentStore` | 记忆文档 | file | postgres |
| `GraphStore` | 实体图 | 内存 / file | neo4j |
| `ApprovalStore` | 审批单持久化 | 内存 | — |
| `DeadLetterStore` | 死信信箱 | 内存 / file | redis |
| `CacheStore` | LLM 响应缓存 | 内存 | redis |
| `LockStore` | 抢锁仲裁（buzz_in） | 进程内 | redis |
| `ExperienceStore` | 经验日志 | file JSONL | postgres |

目录本身只有 851 行——**契约比实现便宜一个数量级，这正是它的价值**：
读契约就知道了行为的边界，不必读实现。

## 为什么先立契约

**第一个原因：可替换性需要事先说清“替换什么”。**
“后端可换”如果只是口号，第一次接 Postgres 时就会发现各处私自调了
file 后端的私有方法。端口把这个承诺压缩成签名：
`backends/factory.py` 的解析表按“端口 × 引擎”查表装配，业务代码
（runtime/plan/coordination）**只在函数体内**经工厂拿实现——
`tests/core/test_layering_contract.py` 用 AST 保证没有任何业务模块在
模块级导入 backends。

**第二个原因：异步必须是契约，不是约定。**
14 个端口的所有方法都是 `async`。这不是风格——Redis/Neo4j/Postgres
驱动都是网络调用，一个同步方法就会阻塞事件循环里所有并发 Agent。
`tests/core/test_port_async_contract.py` 遍历每个 Protocol 的每个成员
断言它是协程或异步生成器：新端口想偷偷加同步方法，先红这条测试。

## 契约的纪律

- **单一归属**：每个类型只在一个文件定义。历史上 `LLMConfig` 有过
  双重归属、`ExperienceRecord` 住在 evaluation 里被端口反向引用——
  都已在重构中归位。现在 `ports/llm.py` 是
  `LLMClient/LLMConfig` 的唯一归属，`llm/` 只做显式 re-export。
- **不承诺实现细节**：`SessionStore.save` 的文档串只说“幂等原子持久化
  + `expected_version` 乐观并发”，不说存成 JSON 还是行——file 与
  postgres 两种实现都必须满足这句话。
- **runtime_checkable**：`isinstance` 结构检查可用，框架由此不 import
  你的实现类也能校验它（`test_factory.py` 对每个解析结果做这个检查）。

## 取舍

**不是 ABC（继承式抽象基类）？** Protocol 是结构化契约：你的类
不需要继承任何框架基类，长得像就算数。这让“把自己的审批系统接进来”
不需要碰框架的类层次——写一个有 `save/load` 方法的对象即可。框架里
唯一的例外是 `AgentError` 异常族（异常需要可捕获的身份，必须继承）。

**为什么 LLMClient 不拆成 stream/complete 两个契约？** 流式不是另一个
能力，是同一次调用的输出方式：`on_chunk` 回调让适配器在内部决定
流式与否，`complete` 的返回值永远是完整的 `LLMResponse`。契约越少，
mock 越容易——测试套件 1,182 个全离线跑，靠的就是“一个 FakeLLM 就能
替换真模型”。

