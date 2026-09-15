# 30 分钟手写一个最小内核

节点、波次、就绪集合这些词，读的时候觉得懂，一合上网页就忘。解药很简单：**自己动手把最小的引擎写一遍**。这一篇完全不 import prodagent，只用 Python 标准库，大约 80 行，你就能写出一个能按“波次”并发跑图的引擎。写完之后，真正的内核在你眼里就不再是一堆名词了。

你只需要知道 `async def` 异步函数是什么。跟着下面五步走，最后有一份拼好的、可以直接运行的完整脚本。

## 三个最小的零件

哪怕是玩具引擎，也绕不开三样东西，它们正好就是真实内核的前三个部件：

- **Plan（蓝图）**：有哪些节点、它们怎么连（静态的、可以反复用）；
- **Run（一次执行）**：这一次跑到哪了、产出了什么数据（动态的、一次性的）；
- **Scheduler（调度器）**：反复只问一个问题——现在哪些节点就绪了？

## 第一步：节点就是一个异步函数

节点干一点活、返回一个值。之所以用 `async`，是因为真实的节点要等模型、等工具、等网络：

```python
async def a(shared):
    return "done by a"
```

`shared` 是到目前为止累积下来的数据，后面你会看到：节点只被允许读到更早波次的结果。

## 第二步：Plan 记下节点和边

蓝图里存每个节点的执行体，以及每个节点的前驱。所谓边，其实就是“在我之前谁得先做完”：

```python
class Plan:
    def __init__(self):
        self._bodies = {}          # 节点名 -> 执行体
        self._pred = {}            # 节点名 -> 它的前驱们

    def add(self, name, body):
        self._bodies[name] = body
        self._pred.setdefault(name, [])
        return self                # 返回 self，方便链式调用

    def edge(self, src, dst):
        self._pred.setdefault(src, [])
        self._pred.setdefault(dst, []).append(src)
        return self
```

`setdefault(key, [])` 是 Python 的一个常见写法：有这个键就用已有的列表，没有就先放一个空列表。没有任何框架魔法。

## 第三步：Run 记下一次执行的进度

蓝图是所有请求共用的，而一次 Run 只属于这一次执行。它记录每个节点跑到什么状态，以及这次执行产出的共享数据：

```python
PENDING, COMPLETED = "pending", "completed"

class Run:
    def __init__(self):
        self.states = {}           # 节点名 -> PENDING / COMPLETED
        self.shared = {}           # 节点名 -> 它返回的值
```

## 第四步：`ready()` 是引擎唯一反复算的事

整个引擎的心脏就在这四行：一个节点**就绪**，当且仅当它还没跑过、并且它的所有前驱都已完成：

```python
def ready(self, run):
    out = []
    for name in self._bodies:
        if run.states.get(name, PENDING) != PENDING:
            continue                       # 已经跑过了
        if all(run.states.get(p) == COMPLETED for p in self._pred[name]):
            out.append(name)               # 前驱都完成了 -> 就绪
    return out
```

一个没有前驱的节点，它的前驱列表是空的；而对空列表求 `all(...)` 结果是 `True`，所以入口节点在第一波就就绪。注意这是一个**只依赖当前 Run 的纯函数**——它不执行任何东西，只回答“谁就绪了”。真实的 `graph.py` 里也是同一个计算，只是额外加上了条件边、扇出和汇聚。

## 第五步：Scheduler 一波一波往前推

引擎的主循环是：算出就绪集合，把这一整波**并发**跑起来，在一个屏障处等它们全部结束，统一提交结果，然后再来一轮；没有节点就绪时就停：

```python
class Scheduler:
    async def drive(self, plan, run):
        wave = 0
        while True:
            ready = plan.ready(run)
            if not ready:
                break                        # 没人可跑了 -> 结束
            wave += 1
            print(f"wave {wave}: {ready}")

            async def run_node(name):
                value = await plan._bodies[name](run.shared)
                run.shared[name] = value     # 只在这里、屏障处提交
                run.states[name] = COMPLETED

            await asyncio.gather(*(run_node(n) for n in ready))
        return run.shared
```

为什么要等屏障处才提交，而不是节点一做完就写？因为同一波的节点是同时跑的，绝不能互相看见对方做了一半的结果。大家都只读之前波次已经提交的数据，这一波结束再一起提交，于是最终结果和“谁先跑完”这种偶然因素无关。就这一条规则，让并发执行的结果变得确定。

## 拼起来，跑一遍

下面是完整脚本，存成 `mini.py`，直接 `python mini.py` 就能跑，不用装任何东西、也不用 API key：

```python
import asyncio

PENDING, COMPLETED = "pending", "completed"


class Plan:
    def __init__(self):
        self._bodies = {}
        self._pred = {}

    def add(self, name, body):
        self._bodies[name] = body
        self._pred.setdefault(name, [])
        return self

    def edge(self, src, dst):
        self._pred.setdefault(src, [])
        self._pred.setdefault(dst, []).append(src)
        return self

    def ready(self, run):
        out = []
        for name in self._bodies:
            if run.states.get(name, PENDING) != PENDING:
                continue
            if all(run.states.get(p) == COMPLETED for p in self._pred[name]):
                out.append(name)
        return out


class Run:
    def __init__(self):
        self.states = {}
        self.shared = {}


class Scheduler:
    async def drive(self, plan, run):
        wave = 0
        while True:
            ready = plan.ready(run)
            if not ready:
                break
            wave += 1
            print(f"wave {wave}: {ready}")

            async def run_node(name):
                value = await plan._bodies[name](run.shared)
                run.shared[name] = value
                run.states[name] = COMPLETED

            await asyncio.gather(*(run_node(n) for n in ready))
        return run.shared


# 图：a -> (b, c) -> d
async def a(shared): return "done by a"
async def b(shared): return f"b read [{shared['a']}]"
async def c(shared): return f"c read [{shared['a']}]"
async def d(shared): return f"d merged [{shared['b']}] & [{shared['c']}]"

plan = Plan()
(plan.add("a", a).add("b", b).add("c", c).add("d", d))
plan.edge("a", "b").edge("a", "c").edge("b", "d").edge("c", "d")

final = asyncio.run(Scheduler().drive(plan, Run()))
print("final shared:", final)
```

输出正好是三波——先 `a` 自己，然后 `b`、`c` 一起，最后才是 `d`：

```text
wave 1: ['a']
wave 2: ['b', 'c']
wave 3: ['d']
final shared: {'a': 'done by a', 'b': 'b read [done by a]', 'c': 'c read [done by a]', 'd': 'd merged [b read [done by a]] & [c read [done by a]]'}
```

你可以改一条边、加一个节点，或者给 `b` 加一句 `await asyncio.sleep(1)` 让它变慢，再看波次形状怎么变。这就是一个引擎最核心的反馈回路。

## 这 80 行，离 prodagent 还差什么

你刚写出的是骨架。真正的内核不是另一套思路，而是在这个骨架上，每多撞一堵墙、就补上一个最小的部件：

| 这个玩具 | prodagent 补上了什么 | 不补会怎样 |
|---|---|---|
| 节点直接写 `shared` | 命名通道 + 合并规则，节点只返回 `state_delta` | 并发写入会互相覆盖、丢数据 |
| 进程一退什么都没了 | 只追加的 `EventLog`，状态是事件折叠出来的 | 没法崩溃恢复、重放、时间旅行 |
| 只能一路跑到黑 | `Interrupt`：先落盘、放手，之后再继续 | 没法在中途停下来等人审批 |
| 引擎闷头跑、外面看不见 | 一条 `Bus`，用来旁观、裁决、订阅 | 外部没法观测，也没法加护栏 |
| 边一开始就写死 | 运行时选边的 `Goto`、动态散开的 `Send` | 循环、交接、运行时才定路数的扇出都做不了 |
| body 只是普通函数 | 四种 body（函数/工具/模型/子图），模型藏在端口后面 | 内核就得认识某一家具体厂商 |

每一行都对应六个部件中的一个，而且每个部件都和你刚推导前三个时一样：先撞上一个具体的墙，再加能搬走它的最小东西，绝不多加。配套专栏会带着你用真实代码，把这几步一个个走完。

## 接下来读什么

- [架构总览](architecture.md)：一张图看全六个部件。
- [设计取舍 01](design/01-plan-and-run.md)：为什么你刚写下的 Plan/Run 分离是必须的，而不是风格偏好。
- 然后去读 `src/kernel/graph.py` 和 `src/kernel/scheduler.py`，你会一眼认出里面的 `ready()` 和波次循环。
