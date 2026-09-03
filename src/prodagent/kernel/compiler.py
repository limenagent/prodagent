"""compiler — the code→graph front-end (column 6).

A workflow body is written as plain control flow — sequence, ``if``,
``while``, a parallel block — and never *executed*: it is a description
the compiler reads with ``ast`` and turns into a :class:`Plan`. The same
runtime graph any hand-written front-end produces, so the scheduler treats
it identically.

The rules, one line each (column 6):

- a written order of steps → **sequence edges**;
- an ``if`` → a **conditional edge** (``when``) on each branch;
- a ``while`` → a **back edge** (the loop turns while the predicate holds);
- ``async with ctx.parallel()`` → **fan-out + all-join**;
- ``return goto(x)`` → a runtime edge choice (a ``Goto`` command).

Data flows through channels, never through variables: a step returns an
``Update`` command into a declared channel and downstream steps read it
(column 7) — control is edges, data is state.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal, cast

from prodagent.kernel.bodies import FnBody
from prodagent.kernel.command import Goto
from prodagent.kernel.graph import Node, Origin, Plan

if TYPE_CHECKING:
    from prodagent.kernel.graph import Plan

__all__ = ["CompileError", "Compiled", "compile", "workflow"]


class CompileError(TypeError):
    """A body the compiler cannot translate — names the offending line.

    The supported subset (column 6's structured form): ``await ctx.call(step)``,
    ``if`` / ``while`` over state predicates, ``async with ctx.parallel()``,
    and ``return`` / ``return goto(x)``. Everything else — bare ``for``,
    ``try``, lambdas, attribute writes — is rejected here, loudly, rather
    than silently compiled wrong."""


@dataclass
class Compiled:
    """What ``compile`` hands back: the plan and its fn table (hand the
    latter to the composition root so FnBody nodes can resolve at run)."""

    plan: Plan
    fns: dict[str, Callable[..., Any]]


def workflow(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Mark a body as a compile-time description — the marker exists for
    discoverability; ``compile`` reads the source either way."""
    fn._prodagent_workflow_body = True  # type: ignore[attr-defined]
    return fn


def compile(body: Callable[..., Any]) -> Compiled:
    """Compile a workflow body (an ``async def``) into a Plan + fn table.

    The body is never called — ``inspect.getsource`` + ``ast`` read it.
    Step names resolve against the body's own globals, so a step is the
    ordinary function the ``await ctx.call(name)`` refers to."""
    src = textwrap.dedent(inspect.getsource(body))
    tree = ast.parse(src)
    if not tree.body or not isinstance(tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise CompileError("workflow body must be an async def function")
    fn_node = tree.body[0]
    compiler = _Compiler(fn_node, body.__globals__)
    compiler.compile(fn_node.body)
    return Compiled(plan=compiler.build(), fns=compiler.fns)


class _Gate:
    """A synthetic join/gate node: runs nothing, carries no value — it
    exists to give a merge point a body (routing is the edges' business)."""

    readonly = True
    kind = "gate"

    @property
    def target(self) -> str:
        return "gate"

    async def run(self, input: Any, ctx: Any) -> Any:
        from prodagent.kernel.body import Outcome

        return Outcome(value=None)


class _Compiler:
    def __init__(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, globals: dict[str, Any]) -> None:
        self._fn = fn
        self._globals = globals
        self._g = Plan(origin=Origin.STATIC)
        self._fns: dict[str, Callable[..., Any]] = {}
        self._n = 0
        self._pending_when: Callable[[Any], bool] | None = None
        self._pending_back: bool | None = None
        self._terminal_ids: set[str] = set()

    @property
    def fns(self) -> dict[str, Callable[..., Any]]:
        return self._fns

    def build(self) -> Plan:
        plan = self._g
        for nid in self._terminal_ids:
            node = plan.nodes[nid]
            plan._nodes[nid] = _replace(node, is_terminal=True)
        return plan

    # ── the driver ───────────────────────────────────────────────────────────

    def compile(self, stmts: list[ast.stmt]) -> None:
        prev: str | None = None
        for s in stmts:
            prev = self._stmt(s, prev)

    def _stmt(self, s: ast.stmt, prev: str | None) -> str | None:
        if isinstance(s, ast.If):
            return self._if(s, prev)
        if isinstance(s, ast.While):
            return self._while(s, prev)
        if isinstance(s, ast.AsyncWith):
            return self._parallel(s, prev)
        if isinstance(s, ast.Return):
            return self._return(s, prev)
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Await):
            call = s.value.value
            if self._is_ctx_call(call):
                return self._call(self._step_name(call), prev)
        raise CompileError(
            f"{self._fn.name}:{s.lineno}: unsupported statement "
            f"{ast.dump(s)[:60]} — the structured subset is await ctx.call(..), "
            "if/while over state, ctx.parallel(), return / return goto(..)"
        )

    # ── statements ──────────────────────────────────────────────────────────

    def _call(self, step_name: str, prev: str | None) -> str:
        node_id = self._fresh(step_name)
        self._add(
            Node(node_id=node_id, body=FnBody(fn=step_name)),
            prev,
            when=self._pending_when,
            back=self._pending_back,
        )
        self._pending_when = None
        self._pending_back = None
        self._fns[step_name] = self._resolve_fn(step_name)
        return node_id

    def _if(self, s: ast.If, prev: str | None) -> str | None:
        cond = self._predicate(s.test)
        not_cond = lambda shared: not cond(shared)
        # Branch heads hang off the *conditional* edge; then/else merge to a
        # join-any gate (exactly one branch runs). The empty else links
        # straight through with the negated condition.
        save = self._pending_when
        then_exit = self._branch(s.body, prev, cond)
        self._pending_when = save
        merge = self._gate(join="any")
        if then_exit is not None:
            self._g.edge(then_exit, merge)
        if s.orelse:
            else_exit = self._branch(s.orelse, prev, not_cond)
            if else_exit is not None:
                self._g.edge(else_exit, merge)
        elif prev is not None:
            self._g.edge(prev, merge, when=not_cond)  # empty else: fall through
        return merge

    def _branch(
        self,
        stmts: list[ast.stmt],
        prev: str | None,
        when: Callable[[Any], bool],
        *,
        back: bool | None = None,
    ) -> str | None:
        """Compile one branch; its head gets ``when`` (and the ``back`` role),
        the rest links normally."""
        save_when = self._pending_when
        save_back = self._pending_back
        self._pending_when = when
        self._pending_back = back
        out = prev
        for s in stmts:
            out = self._stmt(s, out)
        self._pending_when = save_when
        self._pending_back = save_back
        return out

    def _while(self, s: ast.While, prev: str | None) -> str | None:
        cond = self._predicate(s.test)
        not_cond = lambda shared: not cond(shared)
        # A tail gate judges the predicate each pass: the body hangs off a
        # conditional entry edge (forward, gates readiness), and the body's
        # last step turns back to the tail via a *back edge* (requeue). The
        # exit edge is conditional — the follower links only when cond fails.
        tail = self._gate(join="all", sources=[prev] if prev else [])
        body_exit = self._branch(s.body, tail, cond, back=False)  # entry edge: forward
        if body_exit is not None:
            self._g.edge(body_exit, tail, back=True)  # the loop's back edge
        self._pending_when = not_cond
        return tail

    def _parallel(self, s: ast.AsyncWith, prev: str | None) -> str | None:
        if not self._is_parallel(s):
            raise CompileError(f"{self._fn.name}:{s.lineno}: expected ctx.parallel() block")
        children = self._parallel_children(s.body)
        heads = [self._call(name, prev) for name in children]
        return self._gate(join="all", sources=heads) if heads else prev

    def _return(self, s: ast.Return, prev: str | None) -> str | None:
        v = s.value
        if v is None:
            if prev is not None:
                self._terminal_ids.add(prev)
            return prev
        if self._is_goto(v):
            target = self._goto_target(v)
            gate = self._goto_node(target, prev)
            return gate
        # returning a value marks the producing step terminal
        if prev is not None:
            self._terminal_ids.add(prev)
        return prev

    # ── AST helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _is_ctx_call(call: ast.AST) -> bool:
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "ctx"
            and call.func.attr == "call"
        )

    @staticmethod
    def _step_name(call: ast.expr) -> str:
        call = cast(ast.Call, call)
        if call.args and isinstance(call.args[0], ast.Name):
            return call.args[0].id
        raise CompileError("ctx.call(...) needs a step name")

    @staticmethod
    def _is_parallel(s: ast.AsyncWith) -> bool:
        return bool(
            s.items
            and isinstance(s.items[0].context_expr, ast.Call)
            and isinstance(s.items[0].context_expr.func, ast.Attribute)
            and isinstance(s.items[0].context_expr.func.value, ast.Name)
            and s.items[0].context_expr.func.value.id == "ctx"
            and s.items[0].context_expr.func.attr == "parallel"
        )

    @staticmethod
    def _parallel_children(body: list[ast.stmt]) -> list[str]:
        out: list[str] = []
        for s in body:
            if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call):
                c = s.value
                if (
                    isinstance(c.func, ast.Attribute)
                    and isinstance(c.func.value, ast.Name)
                    and c.func.attr == "call"
                    and c.args
                    and isinstance(c.args[0], ast.Name)
                ):
                    out.append(c.args[0].id)
        return out

    @staticmethod
    def _is_goto(v: ast.AST) -> bool:
        return isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "goto"

    @staticmethod
    def _goto_target(v: ast.expr) -> str:
        v = cast(ast.Call, v)
        if v.args and isinstance(v.args[0], ast.Name):
            return v.args[0].id
        if v.args and isinstance(v.args[0], ast.Constant) and isinstance(v.args[0].value, str):
            return v.args[0].value
        raise CompileError("goto(...) needs a step name")

    def _predicate(self, node: ast.expr) -> Callable[[Any], bool]:
        fn = self._expr(node)
        return lambda shared: bool(fn(shared))

    def _expr(self, node: ast.expr) -> Callable[[Any], Any]:
        """Compile a state predicate expression → ``lambda shared: value``.

        The subset: ``s.attr`` reads a channel; constants; ``not``/``and``/
        ``or``; comparisons; ``len(..)``; ``x in y`` / ``not in``."""
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "s":
            return lambda shared: shared.get(node.attr)
        if isinstance(node, ast.Name) and node.id == "s":
            return lambda shared: shared
        if isinstance(node, ast.Constant):
            return lambda shared: node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = self._expr(node.operand)
            return lambda shared: not inner(shared)
        if isinstance(node, ast.BoolOp):
            parts = [self._expr(v) for v in node.values]
            if isinstance(node.op, ast.And):
                return lambda shared: all(p(shared) for p in parts)
            return lambda shared: any(p(shared) for p in parts)
        if isinstance(node, ast.Compare):
            left = self._expr(node.left)
            rights = [self._expr(c) for c in node.comparators]
            ops = node.ops
            def _cmp(shared: dict[str, Any]) -> bool:
                a = left(shared)
                for op, rc in zip(ops, rights):
                    b = rc(shared)
                    if isinstance(op, ast.Eq):
                        if not (a == b):
                            return False
                    elif isinstance(op, ast.NotEq):
                        if not (a != b):
                            return False
                    elif isinstance(op, ast.Lt):
                        if not (a < b):
                            return False
                    elif isinstance(op, ast.LtE):
                        if not (a <= b):
                            return False
                    elif isinstance(op, ast.Gt):
                        if not (a > b):
                            return False
                    elif isinstance(op, ast.GtE):
                        if not (a >= b):
                            return False
                    elif isinstance(op, ast.In):
                        if not (a in b):
                            return False
                    elif isinstance(op, ast.NotIn):
                        if not (a not in b):
                            return False
                    else:
                        raise CompileError(f"unsupported comparison {ast.dump(op)}")
                    a = b
                return True
            return _cmp
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
            inner = self._expr(node.args[0])
            return lambda shared: len(inner(shared))
        raise CompileError(
            f"{self._fn.name}:{node.lineno}: unsupported predicate {ast.dump(node)[:60]}"
        )

    # ── graph construction ──────────────────────────────────────────────────

    def _fresh(self, base: str) -> str:
        self._n += 1
        candidate = base if self._n == 1 else f"{base}_{self._n}"
        used = set(self._g.nodes)
        if candidate in used:
            while candidate in used:
                self._n += 1
                candidate = f"{base}_{self._n}"
        return candidate

    def _add(
        self,
        node: Node,
        prev: str | None,
        *,
        when: Callable[[Any], bool] | None,
        back: bool | None = None,
    ) -> None:
        self._g.add_nodes([node])
        if prev is not None:
            self._g.edge(prev, node.node_id, when=when, back=back)

    def _gate(self, *, join: "Literal[\"all\", \"any\"]", sources: list[str] | None = None) -> str:
        gid = self._fresh("gate")
        self._g.add_nodes([Node(node_id=gid, body=_Gate(), join=join)])
        for src in sources or []:
            self._g.edge(src, gid)
        return gid

    def _goto_node(self, target: str, prev: str | None) -> str:
        gid = self._fresh(f"goto_{target}")
        fn_name = f"__goto_{gid}"
        self._fns[fn_name] = lambda: Goto(target)
        self._add(Node(node_id=gid, body=FnBody(fn=fn_name)), prev, when=self._pending_when)
        self._pending_when = None
        return gid

    def _resolve_fn(self, name: str) -> Callable[..., Any]:
        fn = self._globals.get(name)
        if fn is None:
            raise CompileError(
                f"{self._fn.name}: step {name!r} is not defined in the body's module"
            )
        return fn  # type: ignore[no-any-return]


def _replace(node: Node, **kw: Any) -> Node:
    from dataclasses import replace

    return replace(node, **kw)
