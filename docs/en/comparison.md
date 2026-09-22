# Concept map: from prodagent to mainstream frameworks

If you've used LangGraph, Google ADK, CrewAI, or OpenAI Assistants, this table
builds the bridge fastest. Their surface vocabulary differs, but at the bottom
they handle the same set of problems.

## Core concept map

| prodagent | LangGraph | Google ADK | Rough correspondence / note |
|---|---|---|---|
| Plan (Node/Edge/Channel) | StateGraph (node/edge/Channel+Reducer) | Agent tree + the newer Workflow API | static execution structure |
| Run | thread / invocation | Invocation + Session | one concrete execution and its state |
| Scheduler | Pregel/BSP executor | Runner | engine that drives "what runs next" |
| wave | superstep | one runner scheduling round | consistency and commit boundary |
| Channel + reducer | Channel + Reducer | session.state delta merge | how concurrent writes merge deterministically |
| EventLog (events as truth) | checkpointer (state-snapshot oriented) | Event stream | prodagent treats events as the sole truth, state as a folded projection |
| Outcome.state_delta | node return / Command(update) | EventActions.state_delta | a node's state increment |
| `Goto` | `Command(goto=...)` | routing / transfer_to_agent | runtime edge choice, back-edge, handoff |
| `Send` | `Send(node, arg)` | dynamic sub-task | fan-out whose count is known only at runtime |
| Interrupt / resume | `interrupt()` + `Command(resume=)` | built from external state yourself | pause for a human, resume from checkpoint |
| Bus (fire/check/subscribe) | callbacks/middleware, LangSmith observability, stream | callbacks and events | mount point for observability, approval, budget; subscribe adds bounded-queue streaming and backpressure |
| child Run / SubPlanBody | subgraph | sub_agents / AgentTool | recursively run another graph inside a node |
| call (delegation, returns) | subgraph-as-node, returns a result | AgentTool / task mode | parent stays in control |
| transfer (handoff, no return) | in-graph `goto` to another agent | transfer_to_agent | control leaves and never returns |
| blackboard | shared channels + conditional edges | shared session.state | whoever has the data acts |
| Agent / Workflow facade | prebuilts like `create_react_agent` | LlmAgent / Workflow | ergonomic wrapper outside the kernel |

## Differences in design orientation (trade-offs, not rankings)

- **LangGraph** exposes low-level primitives — graph, channels, checkpoints —
  directly, giving the most control and the richest ecosystem; the price is more
  concepts and a large kernel where beginners can get lost among abstractions.
  prodagent explains the same principles in code small enough to read end to end.
- **Google ADK** is easy to start with, with a complete agent tree and tooling,
  and its newer releases add Workflow capabilities. Its own direction shows that
  "autonomous agent" and "deterministic workflow" converge on a middle ground —
  the middle ground prodagent is designed for.
- **CrewAI / the OpenAI family** draw the "graph" for you with higher-level
  concepts like roles, tasks, and handoffs, which is fast to write; when you need
  precise control over recovery and concurrent consistency, the bottom layer is
  still state, graph, and scheduler.

In one line: **prodagent doesn't compete on feature count; it is the smallest,
readable, re-implementable reference.** Understand the kernel principles here
first, and when you go back to any framework you'll see not an API but the shared
skeleton beneath.

> Frameworks evolve continuously; this table describes conceptual
> correspondences — consult official docs for exact APIs.
