import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from audi28.adapters.deepseek import DeepSeekBackend
from audi28.backend import FakeBackend
from audi28.dispatch import Dispatcher
from audi28.doctor import doctor_report
from audi28.events import EventLog
from audi28.router import RulesRouter
from audi28.sessions import SessionStore, choose_workdir, task_slug
from audi28.web import WebServer
from tests.test_deepseek_adapter import FakeHarness, FakeSession, RecordingOutbox, _wait_for
from tests.test_doctor import _names, _pass_probes, _ready

NY = ZoneInfo("America/New_York")
WHEN = datetime(2026, 11, 21, 15, 4, tzinfo=NY)


def _clock(*moments: datetime):
    pending = list(moments)

    def clock() -> datetime:
        if len(pending) > 1:
            return pending.pop(0)
        return pending[0]

    return clock


def _router() -> RulesRouter:
    return RulesRouter(
        default="deepseek",
        new_task_phrases=("新任务", "开一个新的", "另外一个任务", "new task"),
        not_new_task_phrases=("不是新任务", "接着上一个"),
        self_phrases=("你直接回答", "不用 DeepSeek"),
    )


def _open(tmp_path, text: str, *, when: datetime = WHEN, message_id: str = "msg-new"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    events = EventLog(tmp_path / "events")
    sessions = SessionStore(tmp_path / "sessions", clock=_clock(when))
    backend = FakeBackend()
    dispatcher = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=backend,
        events=events,
        workspace=str(workspace),
    )
    dispatcher.handle(
        {
            "kind": "task",
            "message_id": message_id,
            "session_id": None,
            "target": None,
            "text": text,
        }
    )
    return workspace, events, sessions, backend


def test_slug_uses_ascii_and_falls_back_to_the_clock():
    assert task_slug("Visa Monitor!", WHEN) == "visa-monitor"
    assert task_slug("监控 visa 进度", WHEN) == "visa"
    assert task_slug("Fix the API: v2", WHEN) == "fix-the-api-v2"
    assert task_slug("visa_monitor", WHEN) == "visa-monitor"
    assert task_slug("帮我看签证进度", WHEN) == "task-1504"
    assert task_slug("   ---  ", WHEN) == "task-1504"
    evening = datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc)
    assert task_slug("签证", evening) == "task-2300"
    assert task_slug("HelloWorld" * 5, WHEN) == "helloworld" * 3 + "he"
    assert task_slug(("word " * 8).strip(), WHEN) == "word-word-word-word-word-word-wo"
    long_slug = task_slug(("alpha " * 12).strip(), WHEN)
    assert long_slug == "alpha-alpha-alpha-alpha-alpha-al"
    assert len(long_slug) <= 32
    assert not long_slug.endswith("-")


def test_new_session_folder_uses_the_injected_clock(tmp_path):
    workspace, events, sessions, backend = _open(tmp_path, "新任务：Visa Monitor")
    record = sessions.list()[0]
    created = datetime.fromisoformat(record.created_ts).astimezone(NY)

    assert record.workdir == "2026-11-21/visa-monitor"
    assert record.workdir.startswith(created.strftime("%Y-%m-%d"))
    assert (workspace / record.workdir).is_dir()
    assert backend.starts[0].workdir == str((workspace / record.workdir).resolve())
    saved = json.loads((sessions.directory / f"{record.session_id}.json").read_text(encoding="utf-8"))
    assert saved["workdir"] == "2026-11-21/visa-monitor"
    session_event = next(event for event in events.replay() if event.type == "session")
    assert session_event.data["workdir"] == "2026-11-21/visa-monitor"
    assert str(workspace) not in session_event.data["workdir"]

    chinese, _, chinese_sessions, _backend = _open(
        tmp_path / "zh",
        "新任务：帮我看签证进度",
        when=datetime(2026, 11, 21, 4, 0, tzinfo=timezone.utc),
    )
    assert chinese_sessions.list()[0].workdir == "2026-11-20/task-2300"
    assert (chinese / "2026-11-20" / "task-2300").is_dir()


def test_same_day_collisions_get_a_numeric_suffix(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "2026-11-21" / "visa-monitor").mkdir(parents=True)
    (workspace / "2026-11-21" / "visa-monitor-2").mkdir()
    taken = choose_workdir(workspace, "Visa Monitor", WHEN)
    assert taken == "2026-11-21/visa-monitor-3"
    assert (workspace / taken).is_dir()

    _, _, sessions, _backend = _open(tmp_path / "live", "新任务：visa monitor")
    again = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=FakeBackend(),
        events=EventLog(tmp_path / "live" / "events"),
        workspace=str(tmp_path / "live" / "workspace"),
    )
    again.handle(
        {
            "kind": "task",
            "message_id": "msg-again",
            "session_id": None,
            "target": None,
            "text": "另外一个任务：visa monitor",
        }
    )
    folders = sorted(record.workdir for record in sessions.list(include_closed=True))
    assert folders == ["2026-11-21/visa-monitor", "2026-11-21/visa-monitor-2"]

    first = next(record for record in sessions.list(include_closed=True) if record.workdir.endswith("/visa-monitor"))
    (tmp_path / "live" / "workspace" / first.workdir).rmdir()
    reserved = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=FakeBackend(),
        events=EventLog(tmp_path / "live" / "events-2"),
        workspace=str(tmp_path / "live" / "workspace"),
    )
    reserved.handle(
        {
            "kind": "task",
            "message_id": "msg-reserved",
            "session_id": None,
            "target": None,
            "text": "开一个新的：visa monitor",
        }
    )
    reserved_session = next(record for record in sessions.list() if record.last_task_id == "msg-reserved")
    assert reserved_session.workdir == "2026-11-21/visa-monitor-3"


def test_continued_session_keeps_its_folder_on_a_later_day(tmp_path, caplog):
    day_one = datetime(2026, 11, 21, 15, 4, tzinfo=NY)
    day_two = datetime(2026, 11, 22, 9, 5, tzinfo=NY)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sessions = SessionStore(tmp_path / "sessions", clock=_clock(day_one, day_two))
    events = EventLog(tmp_path / "events")
    backend = FakeBackend()
    dispatcher = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=backend,
        events=events,
        workspace=str(workspace),
    )
    dispatcher.handle(
        {
            "kind": "task",
            "message_id": "msg-1",
            "session_id": None,
            "text": "新任务：visa monitor",
        }
    )
    record = sessions.list()[0]
    caplog.set_level(logging.WARNING)
    dispatcher.handle(
        {
            "kind": "task",
            "message_id": "msg-2",
            "session_id": None,
            "text": "接着上一个，再查一次",
        }
    )

    kept = sessions.get(record.session_id)
    assert kept is not None
    assert kept.workdir == "2026-11-21/visa-monitor"
    assert kept.updated_ts.startswith("2026-11-22")
    assert backend.runs[0].workdir == str((workspace / kept.workdir).resolve())
    assert not (workspace / "2026-11-22").exists()
    assert not any("没有 workdir" in item.message for item in caplog.records)


def test_legacy_session_stays_at_the_workspace_root(tmp_path, caplog):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    note = workspace / "keep.txt"
    note.write_text("stay\n", encoding="utf-8")
    sessions = SessionStore(tmp_path / "sessions", clock=_clock(WHEN))
    created = sessions.create(
        title="旧会话",
        backend="deepseek",
        backend_session_id="fake-legacy",
        workspace=str(workspace),
        task_id=None,
    )
    events = EventLog(tmp_path / "events")
    backend = FakeBackend()
    dispatcher = Dispatcher(
        router=_router(),
        sessions=sessions,
        backend=backend,
        events=events,
        workspace=str(workspace),
    )
    caplog.set_level(logging.WARNING)
    dispatcher.handle(
        {
            "kind": "task",
            "message_id": "msg-legacy",
            "session_id": created.session_id,
            "text": "接着查",
        }
    )

    assert note.read_text(encoding="utf-8") == "stay\n"
    assert note.parent == workspace
    assert list(workspace.glob("20??-??-??")) == []
    assert sessions.get(created.session_id).workdir is None
    assert backend.runs[0].workdir is None
    assert backend.starts == []
    assert any(
        created.session_id in item.message and "没有 workdir" in item.message and "不移动" in item.message
        for item in caplog.records
    )


class _TrackingHarness(FakeHarness):
    def __init__(self) -> None:
        super().__init__()
        self.trace: list[tuple[str, str]] = []

    def start_session(self, session_id: str) -> "_TrackingSession":
        self.started.append(session_id)
        self.known.add(session_id)
        return _TrackingSession(self, session_id)

    def resume_session(self, session_id: str) -> "_TrackingSession":
        self.resumed.append(session_id)
        if session_id not in self.known:
            raise RuntimeError("missing session")
        return _TrackingSession(self, session_id)


class _TrackingSession(FakeSession):
    def run(self, text: str):
        self.harness.trace.append(("run", text))
        return super().run(text)

    def set_working_directory(self, path: str) -> str:
        self.harness.trace.append(("cwd", path))
        return path


def _tracking_backend(
    tmp_path,
    harness: FakeHarness,
    *,
    workspace=None,
) -> tuple[DeepSeekBackend, RecordingOutbox]:
    events = EventLog(tmp_path / "events")
    outbox = RecordingOutbox()
    backend = DeepSeekBackend(
        api_key="sk-test-secret-value",
        workspace=tmp_path / "workspace" if workspace is None else workspace,
        dsh_home=tmp_path / "dsh-home",
        permission_mode="workspace-write",
        provider="deepseek-official",
        model="deepseek-v4-flash",
        profile="sdk",
        initialize_timeout_seconds=30,
        outbox=outbox,
        events=events,
        pending=__import__("audi28.approvals", fromlist=["PendingStore"]).PendingStore(tmp_path / "approvals"),
        harness_factory=harness.accept,
    )
    return backend, outbox


def test_adapter_points_at_the_session_folder_without_moving_the_runtime_cwd(tmp_path):
    harness = _TrackingHarness()
    backend, outbox = _tracking_backend(tmp_path, harness)
    folder = tmp_path / "workspace" / "2026-11-21" / "visa-monitor"
    folder.mkdir(parents=True)
    try:
        handle = backend.start("ses-1", "跑测试", workdir=str(folder))
        _wait_for(lambda: any(kind == "run" and text.endswith("跑测试") for kind, text in harness.trace))
        backend.run(handle, "补一句", audi_session_id="ses-1", workdir=str(folder))
        _wait_for(lambda: any(kind == "run" and text == "补一句" for kind, text in harness.trace))
    finally:
        backend.close()

    assert harness.options["cwd"] == str(tmp_path / "workspace")
    assert harness.options["cwd"] != str(folder)
    assert harness.trace == [
        ("run", "/permission workspace-write"),
        ("cwd", str(folder.resolve())),
        (
            "run",
            "Working folder for this conversation: "
            f"{folder.resolve()}. Create and edit files only there unless the user explicitly asks otherwise.\n\n跑测试",
        ),
        ("cwd", str(folder.resolve())),
        ("run", "补一句"),
    ]
    assert all(folder.name not in message["spoken_text"] for message in outbox.messages)

    resumed_backend, resumed_outbox = _tracking_backend(
        tmp_path / "resume",
        harness,
        workspace=tmp_path / "workspace",
    )
    try:
        resumed_backend.run(handle, "继续", audi_session_id="ses-1", workdir=str(folder))
        _wait_for(lambda: harness.resumed == [handle])
        _wait_for(lambda: any(text.endswith("继续") and "Working folder" in text for _kind, text in harness.trace))
    finally:
        resumed_backend.close()
    assert harness.resumed == [handle]
    assert harness.trace[-3:] == [
        ("run", "/permission workspace-write"),
        ("cwd", str(folder.resolve())),
        (
            "run",
            "Working folder for this conversation: "
            f"{folder.resolve()}. Create and edit files only there unless the user explicitly asks otherwise.\n\n继续",
        ),
    ]
    assert all(str(folder) not in message["spoken_text"] for message in resumed_outbox.messages)


def test_adapter_still_instructs_when_the_sdk_cannot_set_the_directory(tmp_path):
    harness = FakeHarness()
    backend, _outbox = _tracking_backend(tmp_path, harness)
    folder = tmp_path / "workspace" / "2026-11-21" / "visa-monitor"
    folder.mkdir(parents=True)
    try:
        backend.start("ses-1", "跑测试", workdir=str(folder))
        _wait_for(lambda: any("跑测试" in text for _sid, text in harness.prompts))
    finally:
        backend.close()
    user_prompts = [text for _sid, text in harness.prompts if not text.startswith("/permission")]
    assert user_prompts == [
        "Working folder for this conversation: "
        f"{folder.resolve()}. Create and edit files only there unless the user explicitly asks otherwise.\n\n跑测试"
    ]
    assert harness.options["cwd"] == str(tmp_path / "workspace")


def test_dashboard_and_doctor_show_workdir(tmp_path, monkeypatch):
    import threading
    import urllib.request

    sessions = SessionStore(tmp_path / "sessions")
    created = sessions.create(
        title="签证",
        backend="deepseek",
        backend_session_id="session-1",
        workspace=str(tmp_path / "workspace"),
        task_id=None,
        workdir="2026-11-21/visa-monitor",
    )
    server = WebServer("127.0.0.1", 0, token="dash-token", sessions=sessions)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        page = urllib.request.Request(f"http://127.0.0.1:{server.port}/", headers={"Authorization": "Bearer dash-token"})
        with urllib.request.urlopen(page, timeout=1) as response:
            html = response.read().decode("utf-8")
        state_request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/state",
            headers={"Authorization": "Bearer dash-token"},
        )
        with urllib.request.urlopen(state_request, timeout=1) as response:
            state = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert "工作目录" in html
    assert state["sessions"][0]["session_id"] == created.session_id
    assert state["sessions"][0]["workdir"] == "2026-11-21/visa-monitor"

    path, env = _ready(tmp_path / "doctor", monkeypatch)
    SessionStore(tmp_path / "doctor" / "data" / "sessions").create(
        title="签证",
        backend="deepseek",
        backend_session_id="session-1",
        workspace=str(tmp_path / "workspace"),
        task_id=None,
        workdir="2026-11-21/visa-monitor",
    )
    report = doctor_report(path, environ=env, probes=_pass_probes())
    assert "2026-11-21/visa-monitor" in _names(report)["sessions"].detail
    assert report.exit_code == 0
