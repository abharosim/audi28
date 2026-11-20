import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audi28.approval import (
    ApprovalCenter,
    normalize_claude_permission_request,
    normalize_codex_permission_request,
)
from audi28.cli import main
from audi28.config import load_config
from audi28.events import EventLog
from audi28.supervisor import _transport_loop
from tests.conftest import EXAMPLE, write_config
from tests.test_transport import FakeWebhook

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "docs" / "sources" / "approval-hub-harness-interfaces.md").read_text(encoding="utf-8")
SCHEMA = {
    "id",
    "session_id",
    "source_agent",
    "source_label",
    "kind",
    "tool_name",
    "call_id",
    "reason",
    "summary",
    "detail",
    "questions",
    "created_ts",
    "idempotency_key",
}


def _center(tmp_path, clock=None):
    events = EventLog(tmp_path / "events")
    outbox = _MemoryOutbox()
    center = ApprovalCenter(tmp_path / "approvals", events=events, outbox=outbox, clock=clock)
    return center, outbox, events


class _MemoryOutbox:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def enqueue(self, payload: dict) -> str:
        self.messages.append(payload)
        return "out"


def test_claude_and_codex_samples_match_the_schema(tmp_path):
    assert "tool_name" in SOURCE and "tool_input" in SOURCE
    assert "permission_mode" in SOURCE and "permission_suggestions" in SOURCE
    assert "turn_id" in SOURCE
    claude = {
        "tool_name": "Bash",
        "tool_input": {"command": "cat /tmp/secret.txt"},
        "permission_mode": "default",
        "permission_suggestions": [],
    }
    codex = {
        "tool_name": "Bash",
        "tool_input": {"command": "git status", "description": "status at https://example.com/a"},
        "turn_id": "turn-1",
    }
    center, _outbox, events = _center(tmp_path)
    first = center.open_request(normalize_claude_permission_request(claude))
    second = center.open_request(normalize_codex_permission_request(codex))
    for record, agent, label in (
        (first, "claude", "Claude Code"),
        (second, "codex", "Codex"),
    ):
        body = record.as_dict()
        assert SCHEMA <= set(body)
        assert body["source_agent"] == agent
        assert body["source_label"] == label
        assert body["kind"] == "approval"
        assert body["questions"] is None
        assert "/tmp" not in body["summary"]
        assert "https://" not in body["summary"]
        assert "sk-" not in body["summary"]
    assert first.tool_name == "Bash"
    assert first.command() == "cat /tmp/secret.txt"
    assert second.call_id == "turn-1"
    assert second.reason == "status at https://example.com/a"
    assert any(event.type == "approval" and event.data["source_agent"] == "claude" for event in events.replay())
    assert any(event.type == "approval" and event.data["source_agent"] == "codex" for event in events.replay())


def test_duplicate_request_is_not_asked_twice(tmp_path):
    center, outbox, events = _center(tmp_path)
    fields = normalize_claude_permission_request(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "permission_mode": "default",
            "permission_suggestions": [],
        }
    )
    first = center.open_request(fields)
    second = center.open_request(fields)
    assert second.id == first.id
    assert len(outbox.messages) == 1
    assert len([event for event in events.replay() if event.type == "approval"]) == 1


def test_decision_is_idempotent_and_unknown_ids_get_an_error(tmp_path):
    center, outbox, events = _center(tmp_path)
    record = center.create(
        kind="approval",
        session_id="ses-1",
        backend_session_id="session-1",
        rpc_id="7",
        tool_name="shell",
        call_id="call-1",
        text="DeepSeek 在问你：是否允许。",
        spoken="DeepSeek 在问你：是否允许。",
    )
    first = center.decide(record.id, "allow", message="可以")
    second = center.decide(record.id, "deny", message="不行")
    missing = center.decide("apr-missing", "allow")
    assert first.applied is True
    assert first.decision is not None and first.decision.decision == "allow"
    assert second.applied is False
    assert "已经有决定" in (second.error or "")
    assert missing.applied is False
    assert "没有这条" in (missing.error or "")
    stored = center.get(record.id)
    assert stored is not None and stored.decision == "allow"
    assert stored.status == "decided"
    errors = [message for message in outbox.messages if message["kind"] == "error"]
    assert len(errors) == 2
    assert all(message["spoken"] == message["text"] for message in errors)
    assert len([event for event in events.replay() if event.type == "decision"]) == 1
    assert any(event.type == "error" for event in events.replay())


def test_pending_never_times_out_and_restart_reannounces_once(tmp_path):
    start = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    now = {"value": start}
    center, outbox, _events = _center(tmp_path, clock=lambda: now["value"])
    center.create(
        kind="approval",
        session_id="ses-1",
        backend_session_id="session-1",
        rpc_id="7",
        tool_name="shell",
        text="DeepSeek 在问你：是否允许它使用工具 shell。",
        spoken="DeepSeek 在问你：是否允许它使用工具 shell。",
    )
    now["value"] = start + timedelta(hours=5)
    assert center.remind_due(None) == 0
    assert center.list_pending()[0].status == "pending"
    restarted, outbox2, _events2 = _center(tmp_path, clock=lambda: now["value"])
    assert restarted.reannounce_pending() == 1
    assert restarted.reannounce_pending() == 0
    assert outbox2.messages[-1]["reminder"] is True
    assert outbox2.messages[-1]["spoken"].startswith("DeepSeek 在问你：")
    assert restarted.list_pending()[0].status == "pending"
    assert len(outbox.messages) == 1


def test_reminder_resends_without_denying(tmp_path):
    start = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    now = {"value": start}
    center, outbox, _events = _center(tmp_path, clock=lambda: now["value"])
    center.create(
        kind="user_question",
        session_id="ses-1",
        backend_session_id="session-1",
        rpc_id="q-1",
        text="DeepSeek 在问你：要继续吗？",
        spoken="DeepSeek 在问你：要继续吗？",
    )
    now["value"] = start + timedelta(seconds=30)
    assert center.remind_due(10) == 1
    assert outbox.messages[-1]["reminder"] is True
    assert outbox.messages[-1]["spoken"].startswith("DeepSeek 在问你：")
    assert center.list_pending()[0].status == "pending"
    assert center.list_pending()[0].decision is None


def test_cli_lists_and_decides_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = write_config(tmp_path)
    center, _outbox, _events = _center(tmp_path / "data")
    record = center.create(
        kind="approval",
        session_id="ses-1",
        backend_session_id="session-1",
        rpc_id="7",
        tool_name="shell",
        command="pytest -q",
        text="DeepSeek 在问你：是否允许。",
        spoken="DeepSeek 在问你：是否允许。",
    )
    assert main(["--config", str(path), "approvals", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["id"] == record.id
    assert listed["tool_name"] == "shell"
    assert listed["status"] == "pending"
    assert main(["--config", str(path), "approvals", "decide", record.id, "allow", "--note", "可以"]) == 0
    assert "已记下" in capsys.readouterr().out
    again = main(["--config", str(path), "approvals", "decide", record.id, "deny"])
    captured = capsys.readouterr()
    assert again == 1
    assert "已经有决定" in captured.err
    stored = ApprovalCenter(tmp_path / "data" / "approvals").get(record.id)
    assert stored is not None and stored.decision == "allow"
    assert stored.decision_message == "可以"


def test_remind_after_s_is_optional(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "# remind_after_s = 900\n",
        "remind_after_s = 90\n",
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    assert load_config(path).approval.remind_after_s == 90


def test_transport_loop_reannounces_a_pending_request(tmp_path, monkeypatch):
    import threading

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    server = FakeWebhook()
    monkeypatch.setenv("AUDI28_GROKBOT_WEBHOOK_URL", server.url)
    monkeypatch.setenv("AUDI28_GROKBOT_WEBHOOK_KEY", "test-key")
    config = load_config(write_config(tmp_path))
    center = ApprovalCenter(tmp_path / "data" / "approvals", events=EventLog(tmp_path / "data" / "events"))
    center.create(
        kind="approval",
        session_id="ses-1",
        backend_session_id="session-1",
        rpc_id="7",
        tool_name="shell",
        text="DeepSeek 在问你：是否允许它使用工具 shell。",
        spoken="DeepSeek 在问你：是否允许它使用工具 shell。",
    )
    stop = threading.Event()
    thread = threading.Thread(target=_transport_loop, args=(config, stop), daemon=True)
    try:
        thread.start()
        deadline = threading.Event()

        def seen():
            return any(
                json.loads(item["body"]).get("reminder") is True
                and str(json.loads(item["body"]).get("spoken", "")).startswith("DeepSeek 在问你：")
                for item in server.requests
            )

        for _ in range(50):
            if seen():
                deadline.set()
                break
            stop.wait(0.1)
        assert deadline.is_set()
    finally:
        stop.set()
        thread.join(timeout=3)
        server.close()
