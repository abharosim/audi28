import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from audi28.cli import main
from audi28.config import load_config
from audi28.events import EventLog
from audi28.supervisor import _transport_loop
from audi28.transport import Inbox, Outbox
from tests.conftest import write_config

SECRET = "super-secret-webhook-key"


class _Recorder(ThreadingHTTPServer):
    def __init__(self, server_address, handler, *, status: int, delay_s: float):
        super().__init__(server_address, handler)
        self.status = status
        self.delay_s = delay_s
        self.requests: list[dict] = []
        self._lock = threading.Lock()

    def record(self, headers, body: bytes) -> None:
        with self._lock:
            self.requests.append(
                {
                    "authorization": headers.get("Authorization"),
                    "body": body.decode("utf-8"),
                }
            )


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        server: _Recorder = self.server  # type: ignore[assignment]
        server.record(self.headers, body)
        if server.delay_s:
            time.sleep(server.delay_s)
        try:
            self.send_response(server.status)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, fmt: str, *args: object) -> None:
        return


class FakeWebhook:
    def __init__(self, *, status: int = 200, delay_s: float = 0) -> None:
        self._httpd = _Recorder(("127.0.0.1", 0), _Handler, status=status, delay_s=delay_s)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05})
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/hook?token={SECRET}"

    @property
    def requests(self) -> list[dict]:
        return list(self._httpd.requests)

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


@pytest.fixture
def fake_webhook():
    server = FakeWebhook()
    try:
        yield server
    finally:
        server.close()


def _task(message_id: str = "msg-01", **extra: object) -> dict:
    body = {
        "kind": "task",
        "message_id": message_id,
        "session_id": None,
        "target": None,
        "text": "在仓库里跑测试，把失败的补上。",
        "ts": "2026-11-19T22:08:00.000-05:00",
    }
    body.update(extra)
    return body


def _write_json(directory: Path, name: str, body: object) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def _inbox(tmp_path: Path, *, parse_retries: int = 3, max_bytes: int = 1048576) -> tuple[Inbox, EventLog]:
    events = EventLog(tmp_path / "events")
    inbox = Inbox(
        tmp_path / "inbox",
        events,
        parse_retries=parse_retries,
        max_bytes=max_bytes,
        secrets=(SECRET,),
    )
    return inbox, events


def _outbox(tmp_path: Path, url: str | None, *, timeout_s: float = 10, backoff_s: float = 0) -> tuple[Outbox, EventLog]:
    events = EventLog(tmp_path / "events")
    outbox = Outbox(
        tmp_path / "outbox",
        events,
        url=url,
        key=SECRET if url else None,
        timeout_s=timeout_s,
        backoff_s=backoff_s,
        secrets=(SECRET, url or ""),
    )
    return outbox, events


def _types(events: EventLog) -> list[str]:
    return [event.type for event in events.replay()]


def _dead_pid() -> int:
    candidate = os.getpid() + 100000
    while True:
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            candidate += 1
            continue
        candidate += 1


def test_valid_task_is_received_and_processed(tmp_path):
    inbox, events = _inbox(tmp_path)
    _write_json(inbox.root, "one.json", _task())

    messages = inbox.poll_once()

    assert len(messages) == 1
    assert len(list((inbox.root / "processing").glob("*.json"))) == 1
    assert list((inbox.root / "processed").glob("*.json")) == []
    inbox.complete("msg-01")
    stored = list((inbox.root / "processed").glob("*.json"))
    assert len(stored) == 1
    assert list((inbox.root).glob("*.json")) == []
    received = [event for event in events.replay() if event.type == "inbox_received"]
    assert len(received) == 1
    assert received[0].ref == "msg-01"
    assert received[0].source == "inbox"
    assert received[0].data["message"]["text"] == "在仓库里跑测试，把失败的补上。"
    raw = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "events").glob("*.jsonl"))
    assert str(tmp_path) not in raw


def test_duplicate_message_id_is_not_received_twice(tmp_path):
    inbox, events = _inbox(tmp_path)
    _write_json(inbox.root, "one.json", _task())
    inbox.poll_once()
    _write_json(inbox.root, "two.json", _task())

    inbox.poll_once()
    inbox.complete("msg-01")

    assert _types(events).count("inbox_received") == 1
    assert len(list((inbox.root / "processed").glob("*.json"))) == 2
    assert any(event.type == "error" for event in events.replay())


def test_message_id_stays_idempotent_after_restart(tmp_path):
    inbox, events = _inbox(tmp_path)
    _write_json(inbox.root, "one.json", _task("msg-restart"))
    inbox.poll_once()
    restarted = Inbox(inbox.root, events, parse_retries=3, max_bytes=1048576)
    _write_json(inbox.root, "again.json", _task("msg-restart"))

    restarted.poll_once()

    assert _types(events).count("inbox_received") == 1


def test_same_idempotency_key_is_not_received_twice(tmp_path):
    inbox, events = _inbox(tmp_path)
    _write_json(inbox.root, "one.json", _task("msg-a", idempotency_key="same-key"))
    inbox.poll_once()
    _write_json(inbox.root, "two.json", _task("msg-b", idempotency_key="same-key"))

    inbox.poll_once()

    assert _types(events).count("inbox_received") == 1


def test_bad_json_is_quarantined_after_retries(tmp_path):
    inbox, events = _inbox(tmp_path, parse_retries=2)
    (inbox.root).mkdir(parents=True, exist_ok=True)
    (inbox.root / "bad.json").write_text("{not-json", encoding="utf-8")

    inbox.poll_once()
    assert list((inbox.root / "quarantine").glob("*.json")) == []

    inbox.poll_once()

    assert len(list((inbox.root / "quarantine").glob("*.json"))) == 1
    assert any(event.type == "error" for event in events.replay())
    assert not any(event.type == "inbox_received" for event in events.replay())


def test_symlink_is_quarantined_without_reading_the_target(tmp_path):
    inbox, events = _inbox(tmp_path)
    target = tmp_path / "secret-target.txt"
    target.write_text("do-not-follow-this-target", encoding="utf-8")
    inbox.root.mkdir(parents=True, exist_ok=True)
    (inbox.root / "link.json").symlink_to(target)

    inbox.poll_once()

    assert not (inbox.root / "link.json").exists()
    quarantined = list((inbox.root / "quarantine").iterdir())
    assert quarantined
    assert quarantined[0].is_symlink()
    raw = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "events").glob("*.jsonl"))
    assert "do-not-follow-this-target" not in raw


def test_oversize_file_is_quarantined(tmp_path):
    inbox, events = _inbox(tmp_path, max_bytes=32)
    inbox.root.mkdir(parents=True, exist_ok=True)
    (inbox.root / "big.json").write_bytes(b"{" + b"a" * 100)

    inbox.poll_once()

    assert len(list((inbox.root / "quarantine").glob("*.json"))) == 1
    assert any(event.type == "error" for event in events.replay())


def test_two_workers_claim_a_file_once(tmp_path):
    events = EventLog(tmp_path / "events")
    root = tmp_path / "inbox"
    _write_json(root, "race.json", _task("msg-race"))
    barrier = threading.Barrier(2)

    def worker():
        inbox = Inbox(root, events, parse_retries=3, max_bytes=1048576)
        barrier.wait(timeout=2)
        inbox.poll_once()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert _types(events).count("inbox_received") == 1
    assert len(list((root / "processing").glob("*.json"))) == 1


def test_webhook_2xx_moves_the_outbox_item_and_hides_the_secret(tmp_path, fake_webhook):
    outbox, events = _outbox(tmp_path, fake_webhook.url)
    outbox.enqueue({"kind": "result", "text": "测试已经通过。"})

    outbox.flush_once()

    assert len(fake_webhook.requests) == 1
    assert fake_webhook.requests[0]["authorization"] == f"Bearer {SECRET}"
    assert json.loads(fake_webhook.requests[0]["body"])["kind"] == "result"
    assert len(list((outbox.root / "sent").glob("*.json"))) == 1
    assert list((outbox.root / "pending").glob("*.json")) == []
    sent = [event for event in events.replay() if event.type == "webhook" and event.data.get("status") == "sent"]
    assert len(sent) == 1
    raw = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "events").glob("*.jsonl"))
    assert SECRET not in raw
    assert fake_webhook.url not in raw


def test_webhook_non_2xx_records_error_without_a_same_flush_retry(tmp_path):
    server = FakeWebhook(status=500)
    try:
        outbox, events = _outbox(tmp_path, server.url, backoff_s=0)
        outbox.enqueue({"kind": "result", "text": "失败"})
        outbox.flush_once()
        assert len(server.requests) == 1
    finally:
        server.close()

    assert list((outbox.root / "pending").glob("*.json"))
    assert any(event.type == "error" for event in events.replay())
    raw = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "events").glob("*.jsonl"))
    assert SECRET not in raw
    assert server.url not in raw


def test_webhook_timeout_retries_once_then_records_error(tmp_path):
    server = FakeWebhook(delay_s=1.0)
    try:
        outbox, events = _outbox(tmp_path, server.url, timeout_s=0.15, backoff_s=0)
        outbox.enqueue({"kind": "result", "text": "慢"})
        outbox.flush_once()
        assert len(server.requests) == 2
    finally:
        server.close()

    assert list((outbox.root / "pending").glob("*.json"))
    assert any(event.type == "error" for event in events.replay())
    raw = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "events").glob("*.jsonl"))
    assert SECRET not in raw
    assert server.url not in raw


def test_pending_webhook_survives_restart(tmp_path, fake_webhook):
    outbox, events = _outbox(tmp_path, fake_webhook.url)
    outbox.enqueue({"kind": "result", "text": "稍后发"})
    assert fake_webhook.requests == []

    restarted = Outbox(
        outbox.root,
        events,
        url=fake_webhook.url,
        key=SECRET,
        timeout_s=2,
        backoff_s=0,
        secrets=(SECRET, fake_webhook.url),
    )
    restarted.flush_once()

    assert len(fake_webhook.requests) == 1
    assert len(list((outbox.root / "sent").glob("*.json"))) == 1


def test_missing_webhook_env_does_not_call_the_network(tmp_path, fake_webhook):
    outbox, events = _outbox(tmp_path, None)
    # A URL of None must not fall back to the live server, even if a payload is waiting.
    pending = outbox.root / "pending"
    pending.mkdir(parents=True)
    (pending / "left.json").write_text(
        json.dumps({"id": "out-left", "payload": {"kind": "result"}, "attempts": 0}),
        encoding="utf-8",
    )

    outbox.flush_once()

    assert fake_webhook.requests == []
    assert (pending / "left.json").is_file()
    assert not any(event.type == "error" and "http" in json.dumps(event.data) for event in events.replay())


def test_grokbot_test_uses_the_fake_server(tmp_path, monkeypatch, fake_webhook, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AUDI28_GROKBOT_WEBHOOK_URL", fake_webhook.url)
    monkeypatch.setenv("AUDI28_GROKBOT_WEBHOOK_KEY", SECRET)
    path = write_config(tmp_path)

    code = main(["--config", str(path), "grokbot", "test"])

    assert code == 0
    assert len(fake_webhook.requests) == 1
    text = capsys.readouterr().out + capsys.readouterr().err
    assert SECRET not in text
    assert fake_webhook.url not in text


def test_concurrent_flush_does_not_double_send(tmp_path):
    server = FakeWebhook(delay_s=0.4)
    entered = threading.Event()
    real_open = urllib.request.urlopen

    def opener(request, timeout=10):
        entered.set()
        return real_open(request, timeout=timeout)

    try:
        events = EventLog(tmp_path / "events")
        first = Outbox(
            tmp_path / "outbox",
            events,
            url=server.url,
            key=SECRET,
            timeout_s=2,
            backoff_s=0,
            secrets=(SECRET, server.url),
            opener=opener,
        )
        first.enqueue({"kind": "result", "text": "只发一次"})
        second = Outbox(
            first.root,
            events,
            url=server.url,
            key=SECRET,
            timeout_s=2,
            backoff_s=0,
            secrets=(SECRET, server.url),
            opener=opener,
        )

        def flush(outbox: Outbox) -> None:
            outbox.flush_once()

        leader = threading.Thread(target=flush, args=(first,))
        leader.start()
        assert entered.wait(timeout=2)
        follower = threading.Thread(target=flush, args=(second,))
        follower.start()
        leader.join(timeout=3)
        follower.join(timeout=3)
    finally:
        server.close()

    assert not leader.is_alive()
    assert not follower.is_alive()
    assert len(server.requests) == 1
    sent = [event for event in events.replay() if event.type == "webhook" and event.data.get("status") == "sent"]
    assert len(sent) == 1


def test_abandoned_inflight_file_is_sent_by_a_new_outbox(tmp_path, fake_webhook):
    outbox, events = _outbox(tmp_path, fake_webhook.url, backoff_s=0)
    outbox.inflight.mkdir(parents=True)
    dead_owner = _dead_pid()
    (outbox.inflight / "abandoned.json").write_text(
        json.dumps(
            {
                "id": "out-abandoned",
                "payload": {"kind": "result", "text": "上次没发完"},
                "attempts": 0,
                "owner_pid": dead_owner,
            }
        ),
        encoding="utf-8",
    )

    restarted = Outbox(
        outbox.root,
        events,
        url=fake_webhook.url,
        key=SECRET,
        timeout_s=2,
        backoff_s=0,
        secrets=(SECRET, fake_webhook.url),
    )
    restarted.flush_once()

    assert len(fake_webhook.requests) == 1
    assert json.loads(fake_webhook.requests[0]["body"])["text"] == "上次没发完"
    assert list((outbox.root / "sent").glob("*.json"))
    assert list((outbox.root / "inflight").glob("*.json")) == []


def test_live_owner_inflight_file_is_not_stolen(tmp_path, fake_webhook):
    outbox, _events = _outbox(tmp_path, fake_webhook.url, backoff_s=0)
    outbox.inflight.mkdir(parents=True)
    (outbox.inflight / "busy.json").write_text(
        json.dumps(
            {
                "id": "out-busy",
                "payload": {"kind": "result", "text": "别人正在发"},
                "attempts": 0,
                "owner_pid": os.getppid(),
            }
        ),
        encoding="utf-8",
    )

    outbox.flush_once()

    assert fake_webhook.requests == []
    assert (outbox.inflight / "busy.json").is_file()


def test_malformed_pending_outbox_file_is_quarantined(tmp_path, fake_webhook):
    outbox, events = _outbox(tmp_path, fake_webhook.url, backoff_s=0)
    outbox.pending.mkdir(parents=True)
    (outbox.pending / "bad.json").write_text("{not-json", encoding="utf-8")
    (outbox.pending / "nopayload.json").write_text(
        json.dumps({"id": "out-empty"}),
        encoding="utf-8",
    )

    outbox.flush_once()

    assert list(outbox.pending.glob("*.json")) == []
    quarantined = list((outbox.root / "quarantine").glob("*.json"))
    assert len(quarantined) == 2
    assert sum(1 for event in events.replay() if event.type == "error") >= 2
    assert fake_webhook.requests == []


def test_transport_loop_records_supervisor_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AUDI28_GROKBOT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AUDI28_GROKBOT_WEBHOOK_KEY", raising=False)
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    audi = config.root / "data" / "inbox" / "audi"
    audi.parent.mkdir(parents=True)
    audi.write_text("not a directory", encoding="utf-8")
    stop = threading.Event()
    thread = threading.Thread(target=_transport_loop, args=(config, stop))
    thread.start()
    events = EventLog(config.root / "data" / "events")
    found = False
    deadline = time.time() + 2
    try:
        while time.time() < deadline:
            found = any(
                event.type == "error" and event.source == "supervisor" for event in events.replay()
            )
            if found:
                break
            time.sleep(0.05)
    finally:
        stop.set()
        thread.join(timeout=2)

    assert found
    assert not thread.is_alive()


def test_grokbot_test_without_env_makes_no_request(tmp_path, monkeypatch, fake_webhook, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AUDI28_GROKBOT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AUDI28_GROKBOT_WEBHOOK_KEY", raising=False)
    path = write_config(tmp_path)

    code = main(["--config", str(path), "grokbot", "test"])

    assert code == 1
    assert fake_webhook.requests == []
    assert "AUDI28_GROKBOT_WEBHOOK_URL" in capsys.readouterr().out + capsys.readouterr().err
