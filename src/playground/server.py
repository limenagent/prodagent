"""The playground server: standard library only — one command serves a web
page where you pick a scenario, run it, watch the event stream, and approve
inline.

Run: python -m src.playground  (or make play), then open the printed address.
Two layers:
- a background asyncio thread actually runs the Agent/Workflow and subscribes
  to its Bus to collect events;
- a stdlib ThreadingHTTPServer serves the page and the JSON endpoints,
  submitting coroutines to the background loop.
Agent and Workflow both have run/resume and both carry a bus, so one set of
code drives them both.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src import Agent
from src.kernel.trace import build_trace, span_to_dict
from src.playground.scenarios import SCENARIOS, get_scenario
from src.playground.web import PAGE

# —— the background event loop (every agent runs on this one loop) ——
_LOOP: asyncio.AbstractEventLoop | None = None
_SESSIONS: dict[str, dict] = {}


def _start_background_loop():
    global _LOOP
    _LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(_LOOP)
    _LOOP.run_forever()


def _submit(coro):
    fut = asyncio.run_coroutine_threadsafe(coro, _LOOP)
    return fut.result()


def _jsonable(value):
    """Keep the wire JSON-shaped: live objects riding inside event data (a
    ToolCall inside the messages channel) become dicts instead of the repr
    strings a bare json.dumps(default=str) would leave the browser with."""
    tc = type(value).__name__
    if tc == "ToolCall":
        return {"name": value.name, "arguments": value.arguments, "id": value.call_id}
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _serialize(item: dict) -> dict:
    evt = item.get("evt")
    if evt is None:
        # High-frequency bus-direct events (e.g. llm_delta tokens) carry no
        # Event object; their fields sit in place.
        return {
            "seq": 0,
            "kind": item.get("event"),
            "data": _jsonable({k: v for k, v in item.items() if k != "event"}),
        }
    return {
        "seq": evt.seq,
        "kind": evt.kind,
        "data": _jsonable(evt.data or {}),
        "run_id": evt.run_id,
        "parent": evt.parent_id,
    }


async def _pump(sess: dict):
    async for item in sess["sub"]:
        sess["events"].append(_serialize(item))
        evt = item.get("evt")
        if evt is not None:  # keep the raw facts so the trace can fold Runs with timestamps
            sess["raw_events"].append(evt)


async def _drive(sess: dict, user_input: str, resume_value=None):
    sess["status"] = "running"
    runnable = sess["runnable"]
    # Composition root: host the runnable (one shared ledger/bus) and subscribe
    # before driving; run/resume reuse this same Scheduler.
    scheduler = runnable.host()
    sess["scheduler"] = scheduler
    sess["sub"] = scheduler.bus.subscribe()
    pump = asyncio.create_task(_pump(sess))
    try:
        if resume_value is None:
            if sess.get("chat") and sess.get("history"):
                # Multi-turn continuation: same Agent instance + last turn's
                # messages; the conversation state lives on the session.
                result = await runnable.run(user_input, history=sess["history"])
            else:
                result = await runnable.run(user_input)
        else:
            result = await runnable.resume(sess["run_id"], resume_value)
        sess["result"] = result
        sess["run_id"] = result.run_id
        sess["status"] = str(result.run.state)
        sess["output"] = result.output
        if sess.get("chat"):
            sess["history"] = list(getattr(result, "messages", []) or [])
    except Exception as exc:  # the page shows the failure reason directly
        sess["status"] = "failed"
        sess["error"] = repr(exc)
    finally:
        # Drain the last few queued events before closing the subscription.
        await asyncio.sleep(0)
        q = sess["sub"].queue
        while not q.empty():
            item = q.get_nowait()
            sess["events"].append(_serialize(item))
            evt = item.get("evt")
            if evt is not None:
                sess["raw_events"].append(evt)
        sess["sub"].close()
        pump.cancel()


async def _start(scenario_key: str, user_input: str, lang: str = "en") -> str:
    scenario = get_scenario(scenario_key)
    if scenario is None:
        raise ValueError(f"no such scenario: {scenario_key}")
    # The UI language picks the scripted dialog / instruction language too.
    build = scenario["build"]
    runnable = await build(lang) if scenario["is_async"] else build(lang)
    sid = uuid.uuid4().hex[:12]
    # An Agent supports multi-turn chat (send more messages to continue); a
    # Workflow stays a one-shot run.
    sess = {
        "runnable": runnable,
        "events": [],
        "raw_events": [],
        "scheduler": None,
        "status": "created",
        "result": None,
        "run_id": None,
        "output": None,
        "error": None,
        "sub": None,
        "chat": isinstance(runnable, Agent),
        "history": None,
    }
    _SESSIONS[sid] = sess
    asyncio.create_task(_drive(sess, user_input))
    return sid


async def _turn(sid: str, user_input: str) -> bool:
    """The session's next turn: insert a user-input marker event, rerun with history."""
    sess = _SESSIONS[sid]
    if not sess.get("chat"):
        raise ValueError("this scenario is not an Agent; multi-turn chat is unsupported")
    sess["events"].append({"seq": 0, "kind": "user_turn", "data": {"text": user_input}})
    sess["error"] = None
    asyncio.create_task(_drive(sess, user_input))
    return True


async def _resume(sid: str, approved: bool):
    sess = _SESSIONS[sid]
    asyncio.create_task(_drive(sess, "", {"approved": approved}))
    return True


def _collect_artifacts(events: list[dict]) -> list[dict]:
    """Project the newest pointer of each produced file (the Files panel).

    Same law as kernel.latest_artifacts, applied to the session's serialized
    (dict) events — artifacts are a fold of artifact_written facts.
    """
    latest: dict[str, dict] = {}
    for ev in events:
        if ev.get("kind") == "artifact_written":
            pointer = ev.get("data", {})
            name = pointer.get("filename")
            if name:
                latest[name] = pointer
    return list(latest.values())


async def _events(sid: str, since: int):
    sess = _SESSIONS[sid]
    await asyncio.sleep(0)  # give drive/pump a chance to advance
    # The approval question: read it from the run's parked interrupts (the
    # INTERRUPTED event carries node ids only); fall back to scanning events.
    question = ""
    interrupts = getattr(getattr(sess.get("result"), "run", None), "interrupts", None) or {}
    if interrupts:
        question = next(iter(interrupts.values())).question
    else:
        for ev in reversed(sess["events"]):
            if ev["kind"] == "interrupted":
                question = ev["data"].get("question", "")
                break
    return {
        "events": sess["events"][since:],
        "status": sess["status"],
        "question": question,
        "output": sess["output"],
        "error": sess["error"],
        "artifacts": _collect_artifacts(sess["events"]),
        "trace": [span_to_dict(s) for s in build_trace(sess["raw_events"])],
        "chat": sess.get("chat", False),
    }


async def _artifact(sid: str, uri: str) -> tuple[bytes, str, str]:
    """Load one artifact's bytes plus its mime and filename (fold the pointer
    for metadata, the BlobStore for bytes)."""
    sess = _SESSIONS[sid]
    scheduler = sess.get("scheduler")
    if scheduler is None:
        raise ValueError("scheduler not ready")
    data = await scheduler.blobs.load(uri)
    pointer = next(
        (
            e.data
            for e in sess["raw_events"]
            if e.kind == "artifact_written" and e.data.get("uri") == uri
        ),
        {},
    )
    return data, pointer.get("mime", "application/octet-stream"), pointer.get("filename", uri)


_TEXT_MIMES = ("text/", "application/json", "image/svg+xml")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep it quiet
        pass

    def _send(self, obj, ctype="application/json; charset=utf-8", code=200):
        body = (
            obj
            if isinstance(obj, bytes)
            else json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        )
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/scenarios":
            # Both languages travel to the page; the UI toggle picks per field.
            return self._send(
                [
                    {
                        "key": s["key"],
                        "title": s["title"],
                        "desc": s["desc"],
                        "default": s["default"],
                        "title_zh": s["title_zh"],
                        "desc_zh": s["desc_zh"],
                        "default_zh": s["default_zh"],
                    }
                    for s in SCENARIOS
                ]
            )
        if path == "/api/events":
            from urllib.parse import parse_qs

            q = parse_qs(self.path.split("?", 1)[1])
            sid = q.get("sid", [""])[0]
            since = int(q.get("since", ["0"])[0])
            if sid not in _SESSIONS:
                return self._send({"error": "session not found"}, code=404)
            return self._send(_submit(_events(sid, since)))
        if path == "/api/artifact":
            from urllib.parse import parse_qs

            q = parse_qs(self.path.split("?", 1)[1])
            sid, uri = q.get("sid", [""])[0], q.get("uri", [""])[0]
            if sid not in _SESSIONS:
                return self._send({"error": "session not found"}, code=404)
            data, mime, filename = _submit(_artifact(sid, uri))
            if q.get("download"):  # raw bytes with a download header
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return None
            if mime.startswith(_TEXT_MIMES):  # inline preview
                return self._send(
                    {"mime": mime, "filename": filename, "text": data.decode("utf-8", "replace")}
                )
            import base64  # binary: hand the browser data it can show or link

            return self._send(
                {"mime": mime, "filename": filename, "b64": base64.b64encode(data).decode()}
            )
        return self._send({"error": "not found"}, code=404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/start":
                sid = _submit(
                    _start(body.get("scenario", ""), body.get("input", ""), body.get("lang", "en"))
                )
                return self._send({"sid": sid})
            if path == "/api/turn":
                _submit(_turn(body["sid"], body.get("input", "")))
                return self._send({"ok": True})
            if path == "/api/resume":
                _submit(_resume(body["sid"], bool(body.get("approved"))))
                return self._send({"ok": True})
        except Exception as exc:
            return self._send({"error": repr(exc)}, code=500)
        return self._send({"error": "not found"}, code=404)


def serve(host: str = "127.0.0.1", port: int = 8000):
    threading.Thread(target=_start_background_loop, daemon=True).start()
    while _LOOP is None:  # wait for the background loop to be ready
        pass
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}"
    print(f"prodagent playground running at: {url}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main():
    parser = argparse.ArgumentParser(description="prodagent playground")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
