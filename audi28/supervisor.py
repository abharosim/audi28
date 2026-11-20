"""Process supervisor. Keeps the dashboard, inbox watcher, and webhook outbox up."""

from __future__ import annotations

import os
import signal
import threading

from audi28.adapters.deepseek import DeepSeekBackend
from audi28.approval import ApprovalCenter
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
    runtime = _build_runtime(config)
    server = WebServer(
        config.web.host,
        config.web.port,
        token=os.environ.get("AUDI28_DASHBOARD_TOKEN", ""),
        sessions=runtime.sessions,
        approvals=runtime.center,
        events=runtime.events,
        decider=runtime.backend.submit_decision,
    )
    web_thread = threading.Thread(target=server.serve_forever, name="audi28-web")
    transport_thread = threading.Thread(
        target=_run_transport,
        args=(runtime, config, stop),
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
        transport_thread.join(timeout=8)


class _Runtime:
    def __init__(self, **parts: object) -> None:
        self.__dict__.update(parts)


def _build_runtime(config: Config, harness_factory=None) -> _Runtime:
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
    sessions = SessionStore(config.root / "data" / "sessions")
    center = ApprovalCenter(config.root / "data" / "approvals", events=events, outbox=outbox)
    backend = DeepSeekBackend(
        api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        workspace=config.deepseek.workspace,
        dsh_home=config.deepseek.dsh_home,
        permission_mode=config.deepseek.permission_mode,
        provider=config.deepseek.provider,
        model=config.deepseek.model,
        profile=config.deepseek.profile,
        initialize_timeout_seconds=config.deepseek.initialize_timeout_seconds,
        outbox=outbox,
        events=events,
        pending=center,
        sessions=sessions,
        harness_factory=harness_factory,
    )
    dispatcher = Dispatcher(
        router=RulesRouter(
            default=config.route.default,
            new_task_phrases=config.route.new_task_phrases,
            not_new_task_phrases=config.route.not_new_task_phrases,
            self_phrases=config.route.self_phrases,
        ),
        sessions=sessions,
        backend=backend,
        events=events,
        workspace=str(config.deepseek.workspace),
        outbox=outbox,
    )
    return _Runtime(
        events=events,
        inbox=inbox,
        outbox=outbox,
        sessions=sessions,
        center=center,
        backend=backend,
        dispatcher=dispatcher,
        url=url,
        key=key,
    )


def _transport_loop(config: Config, stop: threading.Event, harness_factory=None) -> None:
    _run_transport(_build_runtime(config, harness_factory), config, stop)


def _run_transport(runtime: _Runtime, config: Config, stop: threading.Event) -> None:
    announced = False
    try:
        while not stop.is_set():
            try:
                for message in _messages_to_dispatch(runtime.inbox):
                    runtime.dispatcher.handle(message)
                    message_id = message.get("message_id")
                    if isinstance(message_id, str) and message_id.strip():
                        runtime.inbox.complete(message_id.strip())
                if not announced:
                    runtime.center.reannounce_pending()
                    announced = True
                runtime.center.remind_due(config.approval.remind_after_s)
                runtime.backend.deliver_decisions()
                runtime.outbox.flush_once()
            except Exception as exc:
                # Keep the dashboard up, but do not hide the failure.
                _record_loop_error(runtime.events, exc, secrets=(runtime.url, runtime.key))
            stop.wait(config.inbox.poll_s)
    finally:
        runtime.backend.close()


def _messages_to_dispatch(inbox: Inbox) -> list[dict]:
    chosen: list[dict] = []
    seen: set[str] = set()
    for message in inbox.undispatched_messages() + inbox.poll_once():
        message_id = message.get("message_id")
        if not isinstance(message_id, str) or not message_id.strip() or message_id in seen:
            continue
        seen.add(message_id)
        chosen.append(message)
    return chosen


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
