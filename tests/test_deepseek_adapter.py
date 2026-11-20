import threading
import time
from pathlib import Path

from audi28.adapters.deepseek import DeepSeekBackend
from audi28.approvals import PendingStore
from audi28.dispatch import Dispatcher
from audi28.events import EventLog
from audi28.router import RulesRouter
from audi28.sessions import SessionStore


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

    def close(self) -> None:
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
    assert handle.startswith("session-")
    assert harness.started == [handle]
    assert harness.options["cwd"] == str(tmp_path / "workspace")
    assert (tmp_path / "workspace").is_dir()
    assert any(text.startswith("/permission workspace-write") for _sid, text in harness.prompts)
    assert any(text == "跑测试" for _sid, text in harness.prompts)
    assert any(message["kind"] == "result" and message["text"].startswith("DeepSeek 说：") for message in outbox.messages)
    assert "sk-test-secret-value" not in "".join(path.read_text() for path in (tmp_path / "events").glob("*.jsonl"))
    backend.close()


def test_continue_resumes_after_restart_and_does_not_replace_a_missing_session(tmp_path):
    harness = FakeHarness()
    backend, _outbox, pending, _events = _backend(tmp_path, harness)
    handle = backend.start("ses-1", "第一次")
    restarted, outbox2, _pending2, _events2 = _backend(tmp_path, harness)
    restarted.pending = pending
    restarted.run(handle, "继续")
    assert harness.resumed == [handle]
    assert harness.started == [handle]

    missing = FakeHarness(script="")
    missing.fail_resume = True
    other, outbox3, _p, _e = _backend(tmp_path / "missing", missing)
    other.run("session-does-not-exist", "继续")
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
            thread.join(timeout=2)
        finally:
            backend.close()
        assert not thread.is_alive()
        assert harness.responses[-1][1] == {"outcome": outcome}


def test_user_question_round_trip(tmp_path):
    harness = FakeHarness(script="question")
    backend, outbox, _pending, _events = _backend(tmp_path, harness)
    answers = [{"id": "q1", "selected": ["继续"], "custom": None}]

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
        thread.join(timeout=2)
    finally:
        backend.close()
    assert harness.question_answers == [{"answers": answers}]


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
