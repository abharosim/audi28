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
    assert main(["--config", str(config), "sessions", "list"]) == 0
    assert created.session_id not in capsys.readouterr().out
    assert main(["--config", str(config), "sessions", "close", "ses-missing"]) == 1


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
