import json
import subprocess
import threading
import time
from pathlib import Path

from audi28.adapters.deepseek import DeepSeekBackend
from audi28.approvals import PendingStore
from audi28.config import load_config
from audi28.dispatch import Dispatcher
from audi28.events import EventLog
from audi28.router import RulesRouter
from audi28.sessions import SessionStore
from audi28.supervisor import _transport_loop
from tests.conftest import write_config
from tests.test_transport import FakeWebhook


class FakeResult:
    def __init__(self, text: str) -> None:
        self.final_response = text


class FakeRequest:
    def __init__(self, request_id: int, method: str, payload: dict) -> None:
        self.id = request_id
        self.method = method
        self.payload = payload


class FakeQuestion:
    def __init__(self) -> None:
        self.request_id = "uq-1"
        self.session_id = "session-q"
        self.questions = [{"id": "q1", "question": "要按这个计划继续吗？详情在 /tmp/plan.md"}]


class FakeSession:
    def __init__(self, harness: "FakeHarness", session_id: str) -> None:
        self.harness = harness
        self.id = session_id

    def run(self, text: str) -> FakeResult:
        self.harness.prompts.append((self.id, text))
        if text.startswith("/permission"):
            return FakeResult("")
        if text == self.harness.fail_text:
            raise RuntimeError("boom")
        self.harness.user_started.set()
        if self.harness.on_user is not None:
            self.harness.on_user(self.id)
        if self.harness.block_user is not None:
            assert self.harness.block_user.wait(timeout=2)
        if self.harness.script == "approval":
            self.harness._queue_impl.put(
                FakeRequest(
                    7,
                    "approval/request",
                    {
                        "sessionId": self.id,
                        "toolName": "shell",
                        "callId": "call-1",
                        "reason": "因为 https://example.com/a 和 /tmp/build",
                        "command": "pytest -q",
                    },
                )
            )
            assert self.harness.responded.wait(timeout=2)
        elif self.harness.script == "question":
            answer = self.harness.on_user_question(FakeQuestion(), threading.Event())
            self.harness.question_answers.append(answer)
        return FakeResult("测试已经通过。")


class FakeHarness:
    def __init__(self, script: str = "") -> None:
        self.script = script
        self.client = self
        self._queue_impl = __import__("queue").Queue()
        self.responses: list[tuple] = []
        self.responded = threading.Event()
        self.started: list[str] = []
        self.resumed: list[str] = []
        self.known: set[str] = set()
        self.prompts: list[tuple[str, str]] = []
        self.question_answers: list[dict] = []
        self.on_user_question = None
        self.options: dict = {}
        self.fail_resume = False
        self.block_user: threading.Event | None = None
        self.user_started = threading.Event()
        self.on_user = None
        self.fail_text = ""
        self.closed = False
        self.child: subprocess.Popen | None = None
        self.errors: list[tuple] = []

    def accept(self, **options):
        self.options = options
        self.on_user_question = options.get("on_user_question")
        return self

    def start_session(self, session_id: str) -> FakeSession:
        self.started.append(session_id)
        self.known.add(session_id)
        return FakeSession(self, session_id)

    def resume_session(self, session_id: str) -> FakeSession:
        self.resumed.append(session_id)
        if self.fail_resume or session_id not in self.known:
            raise RuntimeError("missing session")
        return FakeSession(self, session_id)

    def next_request(self):
        return self._queue_impl.get()

    def respond(self, request_id, result) -> None:
        self.responses.append((request_id, result))
        self.responded.set()

    def respond_error(self, request_id, *, code, message, data=None) -> None:
        del data
        self.errors.append((request_id, code, message))

    def close(self) -> None:
        self.closed = True
        child = self.child
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        self._queue_impl.put(None)


class RecordingOutbox:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def enqueue(self, payload: dict) -> str:
        self.messages.append(payload)
        return f"out-{len(self.messages)}"


def _backend(tmp_path: Path, harness: FakeHarness, *, api_key: str = "sk-test-secret-value") -> tuple[DeepSeekBackend, RecordingOutbox, PendingStore, EventLog]:
    events = EventLog(tmp_path / "events")
    outbox = RecordingOutbox()
    pending = PendingStore(tmp_path / "approvals")
    backend = DeepSeekBackend(
        api_key=api_key,
        workspace=tmp_path / "workspace",
        dsh_home=tmp_path / "dsh-home",
        permission_mode="workspace-write",
        provider="deepseek-official",
        model="deepseek-v4-flash",
        profile="sdk",
        initialize_timeout_seconds=30,
        outbox=outbox,
        events=events,
        pending=pending,
        harness_factory=harness.accept,
    )
    return backend, outbox, pending, events


def _wait_for(predicate, timeout: float = 2) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_new_session_uses_workspace_and_permission_mode(tmp_path):
    harness = FakeHarness()
    backend, outbox, _pending, events = _backend(tmp_path, harness)
    handle = backend.start("ses-1", "跑测试")
    _wait_for(lambda: any(text == "跑测试" for _sid, text in harness.prompts))
    _wait_for(lambda: any(message["kind"] == "result" for message in outbox.messages))
    assert handle.startswith("session-")
    assert harness.started == [handle]
    assert harness.options["cwd"] == str(tmp_path / "workspace")
    assert (tmp_path / "workspace").is_dir()
    assert any(text.startswith("/permission workspace-write") for _sid, text in harness.prompts)
    assert any(message["kind"] == "result" and message["text"].startswith("DeepSeek 说：") for message in outbox.messages)
    assert "sk-test-secret-value" not in "".join(path.read_text() for path in (tmp_path / "events").glob("*.jsonl"))
    backend.close()


def test_continue_resumes_after_restart_and_does_not_replace_a_missing_session(tmp_path):
    harness = FakeHarness()
    backend, _outbox, pending, _events = _backend(tmp_path, harness)
    handle = backend.start("ses-1", "第一次")
    _wait_for(lambda: any(text == "第一次" for _sid, text in harness.prompts))
    restarted, outbox2, _pending2, _events2 = _backend(tmp_path, harness)
    restarted.pending = pending
    restarted.run(handle, "继续")
    _wait_for(lambda: harness.resumed == [handle])
    _wait_for(lambda: any(text == "继续" for _sid, text in harness.prompts))
    assert harness.started == [handle]

    missing = FakeHarness(script="")
    missing.fail_resume = True
    other, outbox3, _p, _e = _backend(tmp_path / "missing", missing)
    other.run("session-does-not-exist", "继续")
    _wait_for(lambda: bool(outbox3.messages))
    assert missing.started == []
    assert any(message["spoken"].startswith("DeepSeek") and "/tmp" not in message["spoken"] for message in outbox3.messages)
    backend.close()
    restarted.close()
    other.close()
    assert outbox2.messages


def test_approval_round_trip_allow_and_deny(tmp_path):
    for decision, outcome in (("allow", "allowed-once"), ("deny", "rejected")):
        harness = FakeHarness(script="approval")
        backend, outbox, pending, _events = _backend(tmp_path / decision, harness)
        found: dict = {}

        def work() -> None:
            found["handle"] = backend.start("ses-1", "跑测试")

        thread = threading.Thread(target=work)
        thread.start()
        try:
            _wait_for(lambda: outbox.messages and outbox.messages[-1]["kind"] == "approval")
            message = outbox.messages[-1]
            assert message["text"].startswith("DeepSeek 在问你：")
            assert "shell" in message["text"]
            assert "/tmp/build" in message["text"]
            assert "https://example.com/a" in message["text"]
            assert message["spoken"].startswith("DeepSeek 在问你：")
            assert "/tmp/build" not in message["spoken"]
            assert "https://" not in message["spoken"]
            assert message["spoken"] == message["spoken_text"]
            stored = PendingStore(pending.directory)
            assert stored.list_pending()
            backend.submit_decision(message["request_id"], decision, None)
            _wait_for(lambda: bool(harness.responses))
            thread.join(timeout=2)
        finally:
            backend.close()
        assert harness.responses[-1][1] == {"outcome": outcome}


def test_user_question_round_trip(tmp_path):
    harness = FakeHarness(script="question")
    backend, outbox, _pending, _events = _backend(tmp_path, harness)
    answers = [{"id": "q1", "selected": "继续", "custom": None}]

    def work() -> None:
        backend.start("ses-q", "提问")

    thread = threading.Thread(target=work)
    thread.start()
    try:
        _wait_for(lambda: any(message["kind"] == "user_question" for message in outbox.messages))
        message = next(item for item in outbox.messages if item["kind"] == "user_question")
        assert message["spoken"].startswith("DeepSeek 在问你：")
        assert "/tmp/plan.md" not in message["spoken"]
        assert "/tmp/plan.md" in message["text"]
        backend.submit_decision(message["request_id"], "allow", answers)
        _wait_for(lambda: bool(harness.question_answers))
        thread.join(timeout=2)
    finally:
        backend.close()
    assert harness.question_answers == [{"answers": [{"id": "q1", "selected": ["继续"]}]}]


def test_missing_key_does_not_call_the_sdk(tmp_path):
    events = EventLog(tmp_path / "events")
    outbox = RecordingOutbox()
    pending = PendingStore(tmp_path / "approvals")

    def factory(**_options):
        raise AssertionError("SDK should not be constructed")

    backend = DeepSeekBackend(
        api_key="",
        workspace=tmp_path / "workspace",
        dsh_home=tmp_path / "dsh-home",
        permission_mode="workspace-write",
        provider="deepseek-official",
        model="deepseek-v4-flash",
        profile="sdk",
        initialize_timeout_seconds=30,
        outbox=outbox,
        events=events,
        pending=pending,
        harness_factory=factory,
    )
    sessions = SessionStore(tmp_path / "sessions")
    dispatcher = Dispatcher(
        router=RulesRouter(
            default="deepseek",
            new_task_phrases=("新任务",),
            not_new_task_phrases=("不是新任务",),
            self_phrases=("你直接回答",),
        ),
        sessions=sessions,
        backend=backend,
        events=events,
        workspace=str(tmp_path / "workspace"),
        outbox=outbox,
    )
    dispatcher.handle(
        {
            "kind": "task",
            "message_id": "msg-nokey",
            "session_id": None,
            "target": None,
            "text": "跑测试",
        }
    )
    assert any(event.type == "error" and "DEEPSEEK_API_KEY" in event.data["reason"] for event in events.replay())
    assert outbox.messages[-1]["spoken"] == "DeepSeek 还没配好密钥，现在接不上。"
    assert "http" not in outbox.messages[-1]["spoken"]
    assert "/" not in outbox.messages[-1]["spoken"]


def test_handle_is_saved_before_the_first_prompt(tmp_path):
    harness = FakeHarness()
    backend, _outbox, _pending, _events = _backend(tmp_path, harness)
    sessions = SessionStore(tmp_path / "sessions")
    backend.sessions = sessions
    record = sessions.create(
        title="第一次",
        backend="deepseek",
        backend_session_id="",
        workspace=str(tmp_path / "workspace"),
        task_id=None,
    )
    seen: list[str] = []
    harness.on_user = lambda _session_id: seen.append(
        sessions.get(record.session_id).backend_session_id
    )
    try:
        handle = backend.start(record.session_id, "跑测试")
        _wait_for(lambda: bool(seen))
    finally:
        backend.close()
    assert seen == [handle]
    assert sessions.get(record.session_id).backend_session_id == handle


def test_unsupported_approval_method_is_rejected(tmp_path):
    harness = FakeHarness()
    backend, _outbox, _pending, _events = _backend(tmp_path, harness)
    try:
        backend.start("ses-1", "跑测试")
        _wait_for(lambda: harness.user_started.is_set())
        harness._queue_impl.put(FakeRequest(9, "unknown/method", {}))
        _wait_for(lambda: bool(harness.errors))
    finally:
        backend.close()
    assert harness.errors[-1][1] == -32601


def test_follow_up_waits_for_the_running_prompt(tmp_path):
    harness = FakeHarness()
    harness.block_user = threading.Event()
    backend, _outbox, _pending, _events = _backend(tmp_path, harness)
    try:
        handle = backend.start("ses-1", "第一次")
        _wait_for(lambda: harness.user_started.is_set())
        backend.run(handle, "第二次")
        time.sleep(0.05)
        assert [text for _sid, text in harness.prompts if not text.startswith("/permission")] == ["第一次"]
        harness.block_user.set()
        _wait_for(lambda: "第二次" in [text for _sid, text in harness.prompts])
    finally:
        if harness.block_user is not None:
            harness.block_user.set()
        backend.close()


def test_transport_loop_flushes_the_decision_round_trip(tmp_path, monkeypatch):
    cases = (
        ("approval", "allow", "allowed-once", None),
        ("approval", "deny", "rejected", None),
        ("question", "allow", None, [{"id": "q1", "selected": "yes"}]),
    )
    for script, decision, outcome, answers in cases:
        root = tmp_path / script / decision
        monkeypatch.setenv("HOME", str(root))
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        server = FakeWebhook()
        monkeypatch.setenv("AUDI28_GROKBOT_WEBHOOK_URL", server.url)
        monkeypatch.setenv("AUDI28_GROKBOT_WEBHOOK_KEY", "test-key")
        config = load_config(write_config(root))
        harness = FakeHarness(script=script)
        stop = threading.Event()
        thread = threading.Thread(
            target=_transport_loop,
            args=(config, stop),
            kwargs={"harness_factory": harness.accept},
            daemon=True,
        )
        inbox = root / "data" / "inbox" / "audi"
        inbox.mkdir(parents=True)
        (inbox / "task.json").write_text(
            json.dumps(
                {
                    "kind": "task",
                    "message_id": f"msg-{script}-{decision}",
                    "session_id": None,
                    "target": None,
                    "text": "跑测试",
                }
            ),
            encoding="utf-8",
        )
        try:
            thread.start()
            asking = "approval" if script == "approval" else "user_question"

            def asked(kind=asking):
                return any(_body(item).get("kind") == kind for item in server.requests)

            _wait_for(asked, timeout=5)
            request_id = next(
                _body(item)["request_id"] for item in server.requests if _body(item).get("kind") == asking
            )
            (inbox / "decision.json").write_text(
                json.dumps(
                    {
                        "kind": "decision",
                        "message_id": f"msg-decision-{script}-{decision}",
                        "request_id": request_id,
                        "decision": decision,
                        "answers": answers,
                    }
                ),
                encoding="utf-8",
            )

            def finished():
                return any(_body(item).get("kind") == "result" for item in server.requests)

            _wait_for(finished, timeout=5)
            if script == "approval":
                assert harness.responses[-1][1] == {"outcome": outcome}
            else:
                assert harness.question_answers == [{"answers": [{"id": "q1", "selected": ["yes"]}]}]
        finally:
            stop.set()
            thread.join(timeout=5)
            assert harness.closed
            assert not thread.is_alive()
            server.close()


def test_close_reaps_the_runtime_and_stops_workers(tmp_path):
    harness = FakeHarness(script="approval")
    child = subprocess.Popen(["sleep", "30"])
    harness.child = child
    backend, outbox, _pending, _events = _backend(tmp_path, harness)
    try:
        backend.start("ses-1", "跑测试")
        _wait_for(lambda: any(message["kind"] == "approval" for message in outbox.messages))
        workers = list(backend._workers.values())
        pump = backend._pump
        assert workers and workers[0].is_alive()
        assert pump is not None and pump.is_alive()
        backend.close()
        assert harness.closed
        assert child.poll() is not None
        assert all(not worker.is_alive() for worker in workers)
        assert not pump.is_alive()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=1)
        backend.close()


def test_concurrent_workers_share_one_harness(tmp_path):
    harness = FakeHarness()
    entered: list[int] = []
    gate = threading.Event()

    def factory(**options):
        entered.append(1)
        if len(entered) == 1:
            assert gate.wait(timeout=2)
        return harness.accept(**options)

    events = EventLog(tmp_path / "events")
    backend = DeepSeekBackend(
        api_key="sk-test",
        workspace=tmp_path / "workspace",
        dsh_home=tmp_path / "dsh-home",
        permission_mode="workspace-write",
        provider="deepseek-official",
        model="deepseek-v4-flash",
        profile="sdk",
        initialize_timeout_seconds=30,
        outbox=RecordingOutbox(),
        events=events,
        pending=PendingStore(tmp_path / "approvals"),
        harness_factory=factory,
    )
    try:
        backend.run("session-a", "一")
        backend.run("session-b", "二")
        _wait_for(lambda: len(entered) == 1)
        deadline = time.time() + 0.3
        while time.time() < deadline:
            assert entered == [1]
            time.sleep(0.02)
        gate.set()
        _wait_for(lambda: sorted(harness.resumed) == ["session-a", "session-b"])
        assert entered == [1]
        pumps = [thread for thread in threading.enumerate() if thread.name == "audi28-deepseek-approvals"]
        assert len(pumps) == 1
    finally:
        gate.set()
        backend.close()


def test_a_failed_prompt_leaves_the_worker_for_the_next_job(tmp_path):
    harness = FakeHarness()
    harness.fail_text = "炸"
    backend, outbox, _pending, _events = _backend(tmp_path, harness)
    try:
        handle = backend.start("ses-1", "炸")
        worker = backend._workers[handle]
        _wait_for(lambda: any(message["kind"] == "error" and message["source_label"] == "DeepSeek" for message in outbox.messages))
        spoken = next(message["spoken"] for message in outbox.messages if message["kind"] == "error")
        assert spoken == "DeepSeek 这一轮没做完。"
        assert "/" not in spoken
        backend.run(handle, "第二次")
        _wait_for(lambda: any(text == "第二次" for _sid, text in harness.prompts))
        _wait_for(lambda: any(message["kind"] == "result" for message in outbox.messages))
        assert backend._workers[handle] is worker
        assert worker.is_alive()
    finally:
        backend.close()


def _body(item: dict) -> dict:
    return json.loads(item["body"])
