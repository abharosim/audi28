"""Process supervisor. Keeps the dashboard, inbox watcher, and webhook outbox up."""

from __future__ import annotations

import os
import signal
import threading

from audi28.backend import UnconnectedBackend
from audi28.config import Config
from audi28.dispatch import Dispatcher
from audi28.events import EventLog
from audi28.router import RulesRouter
from audi28.sessions import SessionStore
from audi28.transport import Inbox, Outbox
from audi28.web import WebServer


def supervise(config: Config, *, stop: threading.Event | None = None) -> None:
    """Run until stop is set, or until SIGINT/SIGTERM when stop is omitted."""

    owns_stop = stop is None
    if stop is None:
        stop = threading.Event()
    server = WebServer(config.web.host, config.web.port)
    web_thread = threading.Thread(target=server.serve_forever, name="audi28-web")
    transport_thread = threading.Thread(
        target=_transport_loop,
        args=(config, stop),
        name="audi28-transport",
        daemon=True,
    )
    web_thread.start()
    transport_thread.start()
    try:
        if owns_stop:
            _wait_for_signal(stop)
        else:
            stop.wait()
    finally:
        stop.set()
        server.shutdown()
        web_thread.join(timeout=5)
        transport_thread.join(timeout=5)


def _transport_loop(config: Config, stop: threading.Event) -> None:
    events = EventLog(config.root / "data" / "events")
    url = os.environ.get("AUDI28_GROKBOT_WEBHOOK_URL", "").strip()
    key = os.environ.get("AUDI28_GROKBOT_WEBHOOK_KEY", "").strip()
    inbox = Inbox(
        config.root / "data" / "inbox" / "audi",
        events,
        parse_retries=config.inbox.parse_retries,
        max_bytes=config.inbox.max_bytes,
        secrets=(url, key),
    )
    outbox = Outbox(
        config.root / "data" / "outbox" / "grokbot",
        events,
        url=url or None,
        key=key or None,
        header_name=config.grokbot.header_name,
        header_value=config.grokbot.header_value,
        timeout_s=config.grokbot.timeout_s,
        secrets=(url, key),
    )
    dispatcher = Dispatcher(
        router=RulesRouter(
            default=config.route.default,
            new_task_phrases=config.route.new_task_phrases,
            not_new_task_phrases=config.route.not_new_task_phrases,
            self_phrases=config.route.self_phrases,
        ),
        sessions=SessionStore(config.root / "data" / "sessions"),
        backend=UnconnectedBackend(),
        events=events,
        workspace=str(config.deepseek.workspace),
    )
    while not stop.is_set():
        try:
            for message in inbox.poll_once():
                dispatcher.handle(message)
            outbox.flush_once()
        except Exception as exc:
            # Keep the dashboard up, but do not hide the failure.
            _record_loop_error(events, exc, secrets=(url, key))
        stop.wait(config.inbox.poll_s)


def _record_loop_error(events: EventLog, exc: Exception, *, secrets: tuple[str, ...]) -> None:
    detail = str(exc)
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, "[redacted]")
    try:
        events.append(
            type="error",
            source="supervisor",
            ref=None,
            data={"reason": type(exc).__name__, "detail": detail},
        )
    except Exception:
        return


def _wait_for_signal(stop: threading.Event) -> None:
    def handle(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    stop.wait()
