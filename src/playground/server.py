"""playground 服务器：只用标准库，起一个网页就能选场景、跑、看事件流、在线审批。

运行：python -m src.playground  （或 make play），然后浏览器打开提示的地址。
架构上分两层：
- 一个后台 asyncio 线程，真正跑 Agent/Workflow，并订阅它们的 Bus 收集事件；
- 标准库 ThreadingHTTPServer 负责网页与 JSON 接口，把协程提交到后台线程执行。
Agent 和 Workflow 都有 run/resume、都带 bus，所以这里用同一套代码驱动。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from src import Agent
from src.playground.scenarios import SCENARIOS, get_scenario
from src.playground.web import PAGE

# —— 后台事件循环（agent 全部跑在这一个 loop 里）——
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


def _serialize(item: dict) -> dict:
    evt = item.get("evt")
    if evt is None:
        # 总线直达的高频事件（如 llm_delta 吐字）没有 Event 对象，字段就在原地。
        return {
            "seq": 0,
            "kind": item.get("event"),
            "data": {k: v for k, v in item.items() if k != "event"},
        }
    return {"seq": evt.seq, "kind": evt.kind, "data": evt.data or {}, "parent": evt.parent_id}


async def _pump(sess: dict):
    async for item in sess["sub"]:
        sess["events"].append(_serialize(item))


async def _drive(sess: dict, user_input: str, resume_value=None):
    sess["status"] = "running"
    runnable = sess["runnable"]
    sess["sub"] = runnable.bus.subscribe()
    pump = asyncio.create_task(_pump(sess))
    try:
        if resume_value is None:
            if sess.get("chat") and sess.get("history"):
                # 多轮续聊：同一 Agent 实例 + 上一轮消息，会话状态由会话持有。
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
    except Exception as exc:  # 页面上直接看到失败原因
        sess["status"] = "failed"
        sess["error"] = repr(exc)
    finally:
        # 排空队列里最后几条事件再关订阅。
        await asyncio.sleep(0)
        q = sess["sub"].queue
        while not q.empty():
            sess["events"].append(_serialize(q.get_nowait()))
        sess["sub"].close()
        pump.cancel()


async def _start(scenario_key: str, user_input: str) -> str:
    scenario = get_scenario(scenario_key)
    if scenario is None:
        raise ValueError(f"没有这个场景：{scenario_key}")
    runnable = await scenario["build"]() if scenario["is_async"] else scenario["build"]()
    sid = uuid.uuid4().hex[:12]
    # 返回的是 Agent 就支持多轮对话（继续发消息续聊）；Workflow 保持一次性运行。
    sess = {
        "runnable": runnable,
        "events": [],
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
    """同一会话的下一轮：插一条“用户输入”分隔事件，带着历史再跑一遍。"""
    sess = _SESSIONS[sid]
    if not sess.get("chat"):
        raise ValueError("该场景不是 Agent，不支持多轮对话")
    sess["events"].append({"seq": 0, "kind": "user_turn", "data": {"text": user_input}})
    sess["error"] = None
    asyncio.create_task(_drive(sess, user_input))
    return True


async def _resume(sid: str, approved: bool):
    sess = _SESSIONS[sid]
    asyncio.create_task(_drive(sess, "", {"approved": approved}))
    return True


async def _events(sid: str, since: int):
    sess = _SESSIONS[sid]
    await asyncio.sleep(0)  # 让 drive/pump 有机会推进
    question = ""
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
        "chat": sess.get("chat", False),
    }


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 安静一点
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
            return self._send(
                [
                    {
                        "key": s["key"],
                        "title": s["title"],
                        "desc": s["desc"],
                        "default": s["default"],
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
                sid = _submit(_start(body.get("scenario", ""), body.get("input", "")))
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
    while _LOOP is None:  # 等后台 loop 就绪
        pass
    httpd = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host}:{port}"
    print(f"src playground 已启动：{url}  （Ctrl+C 停止）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


def main():
    parser = argparse.ArgumentParser(description="src playground")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.host, args.port)


if __name__ == "__main__":
    main()
