"""Local dashboard. It only binds 127.0.0.1 and requires a token except for /health."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from audi28.approval import tokens_match

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>audi28</title>
<style>
  body { font: 15px/1.45 ui-sans-serif, sans-serif; margin: 1.5rem; color: #1c1917; background: #faf7f2; }
  h1 { font-size: 1.4rem; margin: 0 0 0.25rem; }
  h2 { font-size: 1.05rem; margin: 1.4rem 0 0.4rem; }
  p.note { color: #57534e; margin-top: 0; }
  table { border-collapse: collapse; width: 100%; background: white; }
  th, td { border: 1px solid #e7e5e4; padding: 0.4rem 0.55rem; text-align: left; vertical-align: top; }
  th { background: #f5f5f4; }
  button { margin-right: 0.35rem; }
  .empty { color: #78716c; }
  code { font-size: 0.92em; }
</style>
</head>
<body>
<h1>audi28</h1>
<p class="note">本机审批。允许只批准这一次，没有永久允许，也不会因为等太久而拒绝。</p>
<h2>会话</h2>
<div id="sessions"></div>
<h2>待审批和提问</h2>
<div id="pending"></div>
<h2>最近结果</h2>
<div id="results"></div>
<script>
async function tick() {
  const response = await fetch("/api/state");
  if (!response.ok) return;
  render(await response.json());
}
function table(headers, rows) {
  const wrap = document.createElement("div");
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "没有。";
    wrap.appendChild(empty);
    return wrap;
  }
  const el = document.createElement("table");
  const head = document.createElement("tr");
  headers.forEach(function (name) {
    const cell = document.createElement("th");
    cell.textContent = name;
    head.appendChild(cell);
  });
  el.appendChild(head);
  rows.forEach(function (cells) { el.appendChild(cells); });
  wrap.appendChild(el);
  return wrap;
}
function cell(text) {
  const el = document.createElement("td");
  el.textContent = text || "";
  return el;
}
function render(data) {
  const sessions = table(["标题", "状态", "最后活跃"], (data.sessions || []).map(function (row) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(row.title));
    tr.appendChild(cell(row.status));
    tr.appendChild(cell(row.updated_ts));
    return tr;
  }));
  const pending = table(["来源", "类型", "工具", "命令或路径", "等待自", ""], (data.pending || []).map(function (row) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(row.source_label));
    tr.appendChild(cell(row.kind));
    tr.appendChild(cell(row.tool_name));
    tr.appendChild(cell(row.command));
    tr.appendChild(cell(row.created_ts));
    const actions = document.createElement("td");
    actions.appendChild(button("允许", row.id, "allow"));
    actions.appendChild(button("拒绝", row.id, "deny"));
    tr.appendChild(actions);
    return tr;
  }));
  const results = table(["时间", "结果"], (data.results || []).map(function (row) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(row.ts));
    tr.appendChild(cell(row.text));
    return tr;
  }));
  replace("sessions", sessions);
  replace("pending", pending);
  replace("results", results);
}
function replace(id, node) {
  const slot = document.getElementById(id);
  slot.replaceChildren(node);
}
function button(label, id, decision) {
  const el = document.createElement("button");
  el.type = "button";
  el.textContent = label;
  el.addEventListener("click", function () { decide(id, decision); });
  return el;
}
async function decide(id, decision) {
  await fetch("/api/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ request_id: id, decision: decision })
  });
  tick();
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""


class WebServer:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        token: str = "",
        sessions: Any = None,
        approvals: Any = None,
        events: Any = None,
        decider: Any = None,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("web.host 只能是 127.0.0.1。")
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.token = token.strip()  # type: ignore[attr-defined]
        self._httpd.sessions = sessions  # type: ignore[attr-defined]
        self._httpd.approvals = approvals  # type: ignore[attr-defined]
        self._httpd.events = events  # type: ignore[attr-defined]
        self._httpd.decider = decider  # type: ignore[attr-defined]
        self.port = int(self._httpd.server_address[1])

    def serve_forever(self) -> None:
        self._httpd.serve_forever(poll_interval=0.1)

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            body = json.dumps({"ok": True, "service": "audi28"}).encode("utf-8")
            self._send(200, "application/json", body)
            return
        if not self._authorized(parsed.query):
            self._send(401, "text/plain; charset=utf-8", "仪表盘需要 AUDI28_DASHBOARD_TOKEN。\n".encode("utf-8"))
            return
        if path == "/api/state":
            body = json.dumps(_state(self.server), ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json", body, cookie=True)
            return
        if path == "/":
            if parse_qs(parsed.query).get("token"):
                self._redirect("/")
                return
            self._send(200, "text/html; charset=utf-8", _PAGE.encode("utf-8"), cookie=True)
            return
        self._send(404, "text/plain; charset=utf-8", b"not found\n")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        parsed = urlparse(self.path)
        if parsed.path != "/api/decide":
            self._send(404, "text/plain; charset=utf-8", b"not found\n")
            return
        if not self._authorized(parsed.query):
            self._send(401, "text/plain; charset=utf-8", "仪表盘需要 AUDI28_DASHBOARD_TOKEN。\n".encode("utf-8"))
            return
        if not _loopback_host(self.headers.get("Host", "")):
            self._send(403, "text/plain; charset=utf-8", "只接受本机 Host。\n".encode("utf-8"))
            return
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in {"same-origin", "none"}:
            self._send(403, "text/plain; charset=utf-8", "拒绝跨站请求。\n".encode("utf-8"))
            return
        content_type = self.headers.get("Content-Type", "")
        if "application/json" not in content_type:
            self._send(415, "text/plain; charset=utf-8", "需要 JSON。\n".encode("utf-8"))
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self._send(400, "text/plain; charset=utf-8", "JSON 无法解析。\n".encode("utf-8"))
            return
        if not isinstance(payload, dict):
            self._send(400, "text/plain; charset=utf-8", "JSON 必须是对象。\n".encode("utf-8"))
            return
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not isinstance(request_id, str) or not request_id.strip():
            self._send(400, "text/plain; charset=utf-8", "缺少 request_id。\n".encode("utf-8"))
            return
        if decision not in {"allow", "deny", "cancel"}:
            self._send(400, "text/plain; charset=utf-8", "决定只能是允许、拒绝或取消。\n".encode("utf-8"))
            return
        answers = payload.get("answers") if isinstance(payload.get("answers"), list) else None
        note = payload.get("note") if isinstance(payload.get("note"), str) else None
        decider = getattr(self.server, "decider", None)
        if decider is None:
            self._send(503, "text/plain; charset=utf-8", "现在不能记下决定。\n".encode("utf-8"))
            return
        result = decider(request_id.strip(), decision, answers, note)
        if getattr(result, "applied", None) is False:
            message = getattr(result, "error", None) or "没有记下这个决定。"
            self._send(409, "text/plain; charset=utf-8", f"{message}\n".encode("utf-8"))
            return
        self._send(200, "application/json", b'{"ok":true}\n', cookie=True)

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        token = getattr(self.server, "token", "")
        if token and _cookie_safe(token):
            self.send_header(
                "Set-Cookie",
                f"audi28_token={token}; HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()

    def _authorized(self, query: str) -> bool:
        expected = getattr(self.server, "token", "")
        presented = _presented_token(self.headers, query)
        return tokens_match(expected, presented)

    def _send(self, status: int, content_type: str, body: bytes, *, cookie: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        token = getattr(self.server, "token", "")
        if cookie and token and _cookie_safe(token):
            self.send_header(
                "Set-Cookie",
                f"audi28_token={token}; HttpOnly; SameSite=Strict; Path=/",
            )
        self.end_headers()
        self.wfile.write(body)


def _presented_token(headers: Any, query: str) -> str:
    header = headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer ") :].strip()
    cookie = headers.get("Cookie", "")
    for part in cookie.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "audi28_token" and value:
            return value
    params = parse_qs(query)
    values = params.get("token") or []
    return values[0] if values else ""


def _loopback_host(host: str) -> bool:
    hostname = host.split(":")[0].strip("[]")
    return hostname in {"127.0.0.1", "localhost"}


def _cookie_safe(token: str) -> bool:
    return bool(token) and all(char not in token for char in " ;\r\n")


def _state(server: Any) -> dict[str, Any]:
    sessions = []
    store = getattr(server, "sessions", None)
    if store is not None:
        for record in store.list():
            sessions.append(
                {
                    "session_id": record.session_id,
                    "title": record.title,
                    "status": record.status,
                    "updated_ts": record.updated_ts,
                }
            )
    pending = []
    approvals = getattr(server, "approvals", None)
    if approvals is not None:
        for record in approvals.list_pending():
            pending.append(
                {
                    "id": record.id,
                    "kind": record.kind,
                    "source_label": record.source_label,
                    "tool_name": record.tool_name or "",
                    "command": record.command() or "",
                    "created_ts": record.created_ts,
                    "summary": record.summary,
                }
            )
    results = []
    events = getattr(server, "events", None)
    if events is not None:
        for event in events.replay():
            if event.type != "reply":
                continue
            text = event.data.get("text") if isinstance(event.data, dict) else ""
            results.append({"ts": event.ts, "text": str(text or ""), "ref": event.ref})
        results = results[-20:]
    return {"sessions": sessions, "pending": pending, "results": results}
