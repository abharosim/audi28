import json

from audi28.backend import FakeBackend
from audi28.cli import main
from audi28.dispatch import Dispatcher
from audi28.events import EventLog
from audi28.router import RulesRouter
from audi28.sessions import SessionStore
from audi28.transport import Inbox
from tests.conftest import write_config


def _router() -> RulesRouter:
    return RulesRouter(
        default="deepseek",
        new_task_phrases=("新任务", "开一个新的", "另外一个任务", "new task"),
        not_new_task_phrases=("不是新任务", "接着上一个"),
        self_phrases=("你直接回答", "不用 DeepSeek"),
    )


def _dispatcher(tmp_path):
    events = EventLog(tmp_path / "events")
    sessions = SessionStore(tmp_path / "sessions")
    backend = FakeBackend()
    dispatcher = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=backend,
        events=events,
        workspace="/tmp/deepseek-workspace",
    )
    return dispatcher, events, sessions, backend


def test_inbox_task_writes_route_and_session_then_calls_backend(tmp_path):
    dispatcher, events, sessions, backend = _dispatcher(tmp_path)
    inbox = Inbox(tmp_path / "inbox", events, parse_retries=2, max_bytes=1048576)
    (inbox.root).mkdir(parents=True)
    (inbox.root / "one.json").write_text(
        json.dumps(
            {
                "kind": "task",
                "message_id": "msg-01",
                "session_id": None,
                "target": None,
                "text": "在仓库里跑测试",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    for message in inbox.poll_once():
        dispatcher.handle(message)

    kinds = [event.type for event in events.replay()]
    assert kinds.index("route") < kinds.index("session")
    route = next(event for event in events.replay() if event.type == "route")
    assert route.ref == "msg-01"
    assert route.data["reason"]
    assert route.data["session_action"] == "new"
    session = next(event for event in events.replay() if event.type == "session")
    assert session.data["reason"] == route.data["reason"]
    assert len(backend.starts) == 1
    assert sessions.list()[0].backend_session_id == backend.starts[0].handle

    follow = {
        "kind": "task",
        "message_id": "msg-02",
        "session_id": None,
        "target": None,
        "text": "不是新任务，把失败补上",
    }
    dispatcher.handle(follow)
    assert len(backend.starts) == 1
    assert len(backend.runs) == 1
    assert backend.runs[0].backend_session_id == backend.starts[0].handle
    assert backend.runs[0].audi_session_id == sessions.list()[0].session_id
    assert backend.runs[0].audi_session_id != backend.runs[0].backend_session_id

    restarted = SessionStore(sessions.directory)
    assert len(restarted.list()) == 1


def test_closed_session_id_is_rejected_without_a_new_backend_call(tmp_path):
    dispatcher, events, sessions, backend = _dispatcher(tmp_path)
    created = sessions.create(
        title="已有",
        backend="deepseek",
        backend_session_id="fake-kept",
        workspace="",
        task_id=None,
    )
    sessions.close(created.session_id)
    before = len(backend.starts) + len(backend.runs)

    dispatcher.handle(
        {
            "kind": "task",
            "message_id": "msg-closed",
            "session_id": created.session_id,
            "target": None,
            "text": "继续",
        }
    )

    assert len(backend.starts) + len(backend.runs) == before
    assert any(event.type == "error" for event in events.replay())
    assert sessions.list() == []


def test_sessions_cli_lists_and_closes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = write_config(tmp_path)
    store = SessionStore(tmp_path / "data" / "sessions")
    created = store.create(
        title="给 CLI 看",
        backend="deepseek",
        backend_session_id="fake-cli",
        workspace="",
        task_id=None,
    )

    assert main(["--config", str(config), "sessions", "list"]) == 0
    listed = capsys.readouterr().out
    assert created.session_id in listed
    assert "fake-cli" in listed

    assert main(["--config", str(config), "sessions", "close", created.session_id]) == 0
    closed = [
        event
        for event in EventLog(tmp_path / "data" / "events").replay()
        if event.type == "session" and event.data.get("action") == "closed"
    ]
    assert closed
    assert closed[0].data.get("reason")
    assert closed[0].ref == created.session_id
    assert main(["--config", str(config), "sessions", "list"]) == 0
    assert created.session_id not in capsys.readouterr().out
    assert main(["--config", str(config), "sessions", "close", "ses-missing"]) == 1


def test_crash_before_dispatch_is_replayed_once(tmp_path):
    events = EventLog(tmp_path / "events")
    inbox = Inbox(tmp_path / "inbox", events, parse_retries=2, max_bytes=1048576)
    inbox.root.mkdir(parents=True)
    (inbox.root / "one.json").write_text(
        json.dumps(
            {
                "kind": "task",
                "message_id": "msg-crash",
                "session_id": None,
                "text": "新任务：跑测试",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert inbox.poll_once()
    assert list((inbox.root / "processing").glob("*.json"))
    assert list((inbox.root / "processed").glob("*.json")) == []

    restarted_inbox = Inbox(inbox.root, events, parse_retries=2, max_bytes=1048576)
    sessions = SessionStore(tmp_path / "sessions")
    backend = FakeBackend()
    dispatcher = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=backend,
        events=events,
        workspace="",
    )
    messages = _once(restarted_inbox)
    assert messages and messages[0]["message_id"] == "msg-crash"
    dispatcher.handle(messages[0])
    dispatcher.handle(messages[0])
    restarted_inbox.complete("msg-crash")

    routes = [event for event in events.replay() if event.type == "route" and event.ref == "msg-crash"]
    assert len(routes) == 1
    assert len(backend.starts) == 1
    assert list((inbox.root / "processed").glob("*.json"))


def _once(inbox: Inbox) -> list[dict]:
    chosen: list[dict] = []
    seen: set[str] = set()
    for message in inbox.undispatched_messages() + inbox.poll_once():
        message_id = message.get("message_id")
        if not isinstance(message_id, str) or message_id in seen:
            continue
        seen.add(message_id)
        chosen.append(message)
    return chosen


def test_close_session_is_not_replayed(tmp_path):
    events = EventLog(tmp_path / "events")
    sessions = SessionStore(tmp_path / "sessions")
    created = sessions.create(
        title="要关掉",
        backend="deepseek",
        backend_session_id="fake-close",
        workspace="",
        task_id=None,
    )
    inbox = Inbox(tmp_path / "inbox", events, parse_retries=2, max_bytes=1048576)
    inbox.root.mkdir(parents=True)
    (inbox.root / "close.json").write_text(
        json.dumps(
            {
                "kind": "close_session",
                "message_id": "msg-close",
                "session_id": created.session_id,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    dispatcher = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=FakeBackend(),
        events=events,
        workspace="",
    )

    def deliver(current: Inbox) -> None:
        for message in _once(current):
            dispatcher.handle(message)
            message_id = message.get("message_id")
            if isinstance(message_id, str):
                current.complete(message_id)

    deliver(inbox)
    restarted = Inbox(inbox.root, events, parse_retries=2, max_bytes=1048576)
    deliver(restarted)

    closed = [
        event
        for event in events.replay()
        if event.type == "session" and event.data.get("action") == "closed"
    ]
    assert len(closed) == 1
    assert any(event.type == "inbox_done" and event.ref == "msg-close" for event in events.replay())
    assert restarted.undispatched_messages() == []


class _CloseOnTouch(SessionStore):
    def touch(self, session_id: str, *, task_id: str | None):
        self.close(session_id)
        return super().touch(session_id, task_id=task_id)


def test_explicit_closed_session_is_rejected(tmp_path):
    events = EventLog(tmp_path / "events")
    sessions = _CloseOnTouch(tmp_path / "sessions")
    created = sessions.create(
        title="会被关掉",
        backend="deepseek",
        backend_session_id="fake-old",
        workspace="",
        task_id=None,
    )
    backend = FakeBackend()
    dispatcher = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=backend,
        events=events,
        workspace="",
    )
    dispatcher.handle(
        {
            "kind": "task",
            "message_id": "msg-race",
            "session_id": created.session_id,
            "target": None,
            "text": "继续改",
        }
    )

    assert backend.runs == []
    assert backend.starts == []
    actions = [event.data.get("action") for event in events.replay() if event.type == "session"]
    assert "continued" not in actions
    assert "created" not in actions
    assert any(event.type == "error" for event in events.replay())
    assert sessions.get(created.session_id).status == "closed"


def test_inbox_decision_goes_through_decide(tmp_path):
    dispatcher, _events, _sessions, backend = _dispatcher(tmp_path)
    seen: dict = {}

    def record(message):
        seen["message"] = dict(message)

    dispatcher._decide = record
    dispatcher.handle(
        {
            "kind": "decision",
            "message_id": "msg-d",
            "request_id": "apr-1",
            "decision": "allow",
        }
    )
    assert seen["message"]["request_id"] == "apr-1"
    assert backend.starts == []


def test_self_phrase_does_not_call_the_backend(tmp_path):
    dispatcher, events, _sessions, backend = _dispatcher(tmp_path)
    dispatcher.handle(
        {
            "kind": "task",
            "message_id": "msg-self",
            "session_id": None,
            "target": None,
            "text": "你直接回答",
        }
    )
    assert backend.starts == []
    assert backend.runs == []
    route = next(event for event in events.replay() if event.type == "route")
    assert route.data["target"] == "audi_self"
