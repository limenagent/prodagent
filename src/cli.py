"""cli — the prodagent command line: run, web, trace, version.

Zero third-party dependencies (argparse plus the in-process runtime). Four verbs:

    prodagent run "task"       drive the built-in offline flow end to end,
                               answering its approval prompt in the terminal
    prodagent web              launch the local playground (graph/trace/artifacts)
    prodagent trace --dir D    render a durable Run's causal trace from disk
    prodagent version

The CLI is only a thin reader over the same primitives: ``run`` drives a
Workflow and resumes its suspension; ``trace`` projects an EventLog through
render_trace. Nothing here is a second engine.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from src import __version__
from src.backends.file_store import FileEventLog
from src.kernel import render_trace
from src.playground.demo import build_demo

_DEFAULT_TASK = "Check order O-1234; it is overdue and I'd like a refund."


def _ask_yes_no(prompt: str) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except EOFError:  # no interactive stdin: fail closed, never auto-approve
        return False
    return answer in ("y", "yes")


async def _cmd_run(args: argparse.Namespace) -> int:
    wf = build_demo()
    result = await wf.run(args.task)
    if result.status == "suspended":
        approved = args.yes or _ask_yes_no("Approve the refund? [y/N] ")
        result = await wf.resume(result.run_id, {"approved": approved})
    if args.trace:
        print(render_trace(await wf.host().eventlog.all_events()))
    else:
        print(result.output)
    return 0 if result.status == "completed" else 1


def _cmd_web(args: argparse.Namespace) -> int:
    try:
        from src.playground.server import serve
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.split(".")[0] == "examples":
            # the scenario graphs live in the repo's examples package, which is
            # deliberately not bundled in the wheel (see src/playground/scenarios.py)
            print(
                "the playground runs from a repository checkout — the examples "
                "package is not bundled with an installed prodagent"
            )
            return 1
        raise
    print(f"prodagent playground → http://{args.host}:{args.port}")
    serve(args.host, args.port)  # blocks
    return 0


async def _cmd_trace(args: argparse.Namespace) -> int:
    events = await FileEventLog(args.dir).all_events()
    if not events:
        print(f"no recorded runs in {args.dir}")
        return 1
    print(render_trace(events))
    return 0


def _cmd_version(_args: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prodagent",
        description="A tiny, teachable yet production-shaped agent runtime.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the built-in offline flow")
    run.add_argument("task", nargs="?", default=_DEFAULT_TASK)
    run.add_argument("--yes", action="store_true", help="auto-approve any suspension")
    run.add_argument(
        "--trace", action="store_true", help="print the trace tree instead of the output"
    )
    run.set_defaults(func=_cmd_run)

    web = sub.add_parser("web", help="launch the web playground")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.set_defaults(func=_cmd_web)

    trace = sub.add_parser("trace", help="render a durable trace from an event-log directory")
    trace.add_argument("--dir", default=".prodagent/events")
    trace.set_defaults(func=_cmd_trace)

    version = sub.add_parser("version", help="print the prodagent version")
    version.set_defaults(func=_cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Whether a verb runs on the event loop is read from the function rather
    # than a parallel flag — one fewer fact to keep in sync.
    if asyncio.iscoroutinefunction(args.func):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
