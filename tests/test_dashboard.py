import json
import threading
import urllib.error
import urllib.request

from audi28.approval import ApprovalCenter
from audi28.events import EventLog
from audi28.sessions import SessionStore
from audi28.web import WebServer


def _request_no_redirect(port: int, path: str):
    class _Stop(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

    opener = urllib.request.build_opener(_Stop)
    try:
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=1) as response:
            return response.status, response.read().decode("utf-8"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


def _request(port: int, path: str, *, token: str = "", method: str = "GET", payload: dict | None = None, headers: dict | None = None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            return response.status, response.read().decode("utf-8"), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), exc.headers


def test_dashboard_requires_token_and_uses_the_decision_path(tmp_path):
    events = EventLog(tmp_path / "events")
    sessions = SessionStore(tmp_path / "sessions")
    sessions.create(
        title="跑测试",
        backend="deepseek",
        backend_session_id="session-1",
        workspace=str(tmp_path / "workspace"),
        task_id=None,
    )
    center = ApprovalCenter(tmp_path / "approvals", events=events)
    record = center.create(
        kind="approval",
        session_id="ses-1",
        backend_session_id="session-1",
        rpc_id="7",
        tool_name="shell",
        command="pytest -q /tmp/build",
        text="DeepSeek 在问你：是否允许。",
        spoken="DeepSeek 在问你：是否允许。",
    )
    events.append(type="reply", source="deepseek", ref="ses-1", data={"kind": "result", "text": "DeepSeek 说：做完了。"})
    calls: list[tuple] = []

    def decider(request_id, decision, answers, note):
        calls.append((request_id, decision, answers, note))
        return center.decide(request_id, decision, answers, message=note, source="user")

    server = WebServer(
        "127.0.0.1",
        0,
        token="dash-token",
        sessions=sessions,
        approvals=center,
        events=events,
        decider=decider,
    )
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        health, _body, _headers = _request(server.port, "/health")
        assert health == 200
        denied, _text, _headers = _request(server.port, "/")
        assert denied == 401
        page, html, _headers = _request(server.port, "/", token="dash-token")
        assert page == 200
        assert "待审批" in html
        status, _body, headers = _request_no_redirect(server.port, "/?token=dash-token")
        assert status == 302
        assert headers["Location"] == "/"
        assert "dash-token" not in headers["Location"]
        assert "audi28_token=dash-token" in headers["Set-Cookie"]
        state_code, raw, _headers = _request(server.port, "/api/state", token="dash-token")
        assert state_code == 200
        state = json.loads(raw)
        assert state["sessions"][0]["title"] == "跑测试"
        assert state["sessions"][0]["status"] == "open"
        assert state["pending"][0]["tool_name"] == "shell"
        assert state["pending"][0]["command"] == "pytest -q /tmp/build"
        assert state["pending"][0]["source_label"] == "DeepSeek"
        assert state["results"][-1]["text"] == "DeepSeek 说：做完了。"
        blocked, _text, _headers = _request(
            server.port,
            "/api/decide",
            token="dash-token",
            method="POST",
            payload={"request_id": record.id, "decision": "allow", "note": "可以"},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert blocked == 403
        assert calls == []
        allowed, _text, _headers = _request(
            server.port,
            "/api/decide",
            token="dash-token",
            method="POST",
            payload={"request_id": record.id, "decision": "allow", "note": "可以"},
        )
        assert allowed == 200
        assert calls == [(record.id, "allow", None, "可以")]
        stored = center.get(record.id)
        assert stored is not None and stored.decision == "allow"
        again, message, _headers = _request(
            server.port,
            "/api/decide",
            token="dash-token",
            method="POST",
            payload={"request_id": record.id, "decision": "deny"},
        )
        assert again == 409
        assert "已经有决定" in message
        assert center.get(record.id).decision == "allow"
    finally:
        server.shutdown()
        thread.join(timeout=2)
