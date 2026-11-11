"""Deterministic OpenAI-compatible stub for the Phase 0 runtime spike.

Not part of the plugin. Scripted behavior, keyed on the newest message:

- newest message is a tool result      -> text reply quoting the result
- newest user text contains CALL <tool> <json-args>
                                        -> one tool call to <tool>
- request looks like a compaction/summary request
                                        -> a fixed summary
- anything else                         -> "echo: <text>"

Every request body is appended to $STUB_LOG (JSONL) so the spike can prove what
actually reached the model (injected banner text, replayed history, etc.).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("STUB_LOG", "stub_requests.jsonl")
MODEL = "stub-model"
CALL_RE = re.compile(r"CALL\s+(\w+)\s*(\{.*?\})?\s*$", re.S | re.M)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _decide(body: dict) -> dict:
    msgs = body.get("messages") or []
    last = msgs[-1] if msgs else {}
    joined = " ".join(_text(m.get("content")) for m in msgs[-3:]).lower()
    if "summar" in joined and ("compact" in joined or "conversation" in joined) and not body.get("tools"):
        return {"text": "SUMMARY: stub compaction summary of earlier turns."}
    if last.get("role") == "tool":
        return {"text": "tool said: " + _text(last.get("content"))[:300]}
    user = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
    utext = _text(user.get("content"))
    # Only the user's own words: injected plugin context is appended after them.
    m = CALL_RE.search(utext.split("\n\n")[0])
    if m and body.get("tools"):
        names = {t.get("function", {}).get("name") for t in body["tools"]}
        tool, args = m.group(1), m.group(2) or "{}"
        if tool not in names and "tool_call" in names:
            # Deferred plugin tool: go through the Tool Search bridge like a real model would.
            return {"tool": "tool_call", "args": json.dumps(
                {"calls": [{"name": tool, "arguments": json.loads(args)}]})}
        return {"tool": tool, "args": args}
    return {"text": "echo: " + utext.split("\n\n")[0][:200]}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - keep stdout quiet
        pass

    def _send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send_json({"object": "list", "data": [
                {"id": MODEL, "object": "model", "context_length": 200000, "owned_by": "stub"}]})
        self._send_json({"ok": True})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        decision = _decide(body)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "path": self.path, "decision": decision,
                                "body": body}) + "\n")
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        ptok = max(100, len(json.dumps(body.get("messages") or [])) // 4)
        usage = {"prompt_tokens": ptok, "completion_tokens": 10, "total_tokens": ptok + 10}
        if "tool" in decision:
            call = {"id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                    "function": {"name": decision["tool"], "arguments": decision["args"]}}
            msg, finish = {"role": "assistant", "content": None, "tool_calls": [call]}, "tool_calls"
        else:
            msg, finish = {"role": "assistant", "content": decision["text"]}, "stop"
        if not body.get("stream"):
            return self._send_json({"id": cid, "object": "chat.completion", "created": int(time.time()),
                                    "model": MODEL, "choices": [{"index": 0, "message": msg,
                                                                 "finish_reason": finish}], "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta, fin=None, extra=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()),
                   "model": MODEL, "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
            if extra:
                obj.update(extra)
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        chunk({"role": "assistant"})
        if "tool_calls" in msg:
            tc = msg["tool_calls"][0]
            chunk({"tool_calls": [{"index": 0, "id": tc["id"], "type": "function",
                                   "function": {"name": tc["function"]["name"], "arguments": ""}}]})
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": tc["function"]["arguments"]}}]})
        else:
            chunk({"content": msg["content"]})
        chunk({}, finish)
        self.wfile.write(b"data: " + json.dumps({"id": cid, "object": "chat.completion.chunk",
                                                  "model": MODEL, "choices": [], "usage": usage}).encode()
                         + b"\n\ndata: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18555
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
