"""Process supervisor. Keeps the dashboard, inbox watcher, and webhook outbox up."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

from audi28.adapters.deepseek import DeepSeekBackend
from audi28.approval import ApprovalCenter
from audi28.config import Config
from audi28.dispatch import Dispatcher
from audi28.events import EventLog
from audi28.router import RulesRouter
from audi28.sandbox import prepare_sandbox
from audi28.sessions import SessionStore
from audi28.transport import Inbox, Outbox
from audi28.web import WebServer


def supervise(
    config: Config,
    *,
    stop: threading.Event | None = None,
    harness_factory=None,
    reexec=None,
    sync_runner=None,
) -> None:
    """Run until stop is set, or until SIGINT/SIGTERM when stop is omitted.

    ``data/restart.request`` stops this process and executes it again, so a
    code update does not need Ctrl+C. A file already present at startup is
    removed and does not count as a new request.
    """

    owns_stop = stop is None
    if stop is None:
        stop = threading.Event()
    trigger = config.root / "data" / "restart.request"
    trigger.parent.mkdir(parents=True, exist_ok=True)
    trigger.unlink(missing_ok=True)
    restarts = _restart_count()
    last_reason = os.environ.get("AUDI28_RESTART_REASON", "")
    restart: dict[str, object] = {"go": False, "reason": ""}
    deps_mtime = _pyproject_mtime(config.root)
    runtime = _build_runtime(config, harness_factory)
    _write_supervisor_state(
        config.root,
        port=config.web.port,
        restarts=restarts,
        last_reason=last_reason,
    )
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
        args=(runtime, config, stop, restart),
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
        _clear_supervisor_state(config.root)
        if restart["go"]:
            _reexec_supervisor(
                config,
                runtime,
                reason=str(restart["reason"]),
                restarts=restarts,
                deps_mtime=deps_mtime,
                reexec=reexec,
                sync_runner=sync_runner,
            )


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
    sandbox = prepare_sandbox(
        enabled=config.deepseek.sandbox.enabled,
        home=_home_dir(),
        workspace=config.deepseek.workspace,
        dsh_home=config.deepseek.dsh_home,
        project_root=config.root,
        extra_read_paths=config.deepseek.sandbox.extra_read_paths,
    )
    if sandbox.warning:
        events.append(type="error", source="sandbox", ref=None, data={"reason": sandbox.warning})
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
        dsh_bin=sandbox.dsh_bin,
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


def _run_transport(
    runtime: _Runtime,
    config: Config,
    stop: threading.Event,
    restart: dict[str, object] | None = None,
) -> None:
    announced = False
    trigger = config.root / "data" / "restart.request"
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
                _write_supervisor_state(
                    config.root,
                    port=config.web.port,
                    restarts=_restart_count(),
                    last_reason=os.environ.get("AUDI28_RESTART_REASON", ""),
                )
            except Exception as exc:
                # Keep the dashboard up, but do not hide the failure.
                _record_loop_error(
                    runtime.events,
                    exc,
                    secrets=_secret_values(runtime),
                )
            if restart is not None:
                reason = _take_restart(trigger)
                if reason is not None:
                    restart["go"] = True
                    restart["reason"] = reason
                    stop.set()
                    return
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


def _home_dir() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home())).expanduser()


def _write_supervisor_state(root: Path, *, port: int, restarts: int = 0, last_reason: str = "") -> None:
    path = root / "data" / "supervisor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "heartbeat": datetime.now().astimezone().isoformat(timespec="seconds"),
        "port": port,
        "restarts": restarts,
        "last_reason": last_reason,
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _clear_supervisor_state(root: Path) -> None:
    try:
        (root / "data" / "supervisor.json").unlink()
    except FileNotFoundError:
        return


def _take_restart(trigger: Path) -> str | None:
    if not trigger.is_file():
        return None
    try:
        reason = trigger.read_text(encoding="utf-8").strip()[:200]
    except OSError:
        reason = ""
    try:
        trigger.unlink()
    except FileNotFoundError:
        pass
    return reason


def _reexec_supervisor(
    config: Config,
    runtime: _Runtime,
    *,
    reason: str,
    restarts: int,
    deps_mtime: float,
    reexec,
    sync_runner,
) -> None:
    cleaned = _redact_secrets(reason, _secret_values(runtime))[:200]
    os.environ["AUDI28_RESTARTS"] = str(restarts + 1)
    os.environ["AUDI28_RESTART_REASON"] = cleaned
    try:
        runtime.events.append(
            type="supervisor",
            source="supervisor",
            ref=None,
            data={"action": "restart", "reason": cleaned},
        )
    except Exception:
        pass
    _sync_dependencies(config, deps_mtime, sync_runner)
    if reexec is None:
        os.execv(sys.executable, [sys.executable, *sys.argv])
        return
    reexec()


def _sync_dependencies(config: Config, deps_mtime: float, sync_runner) -> None:
    if _pyproject_mtime(config.root) == deps_mtime:
        return
    command = ["uv", "sync", "--extra", "dev"]
    if config.deepseek.enabled:
        command.extend(["--extra", "deepseek"])
    if sync_runner is not None:
        sync_runner(command, config.root)
        return
    if shutil.which("uv") is None:
        return
    subprocess.run(command, cwd=config.root, check=False)


def _pyproject_mtime(root: Path) -> float:
    path = root / "pyproject.toml"
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _restart_count() -> int:
    raw = os.environ.get("AUDI28_RESTARTS", "0").strip()
    if raw.isdigit():
        return int(raw)
    return 0


def _secret_values(runtime: _Runtime) -> tuple[str, ...]:
    return tuple(
        value
        for value in (
            runtime.url,
            runtime.key,
            os.environ.get("AUDI28_DASHBOARD_TOKEN", "").strip(),
            os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        )
        if value
    )


def _redact_secrets(text: str, secrets: tuple[str, ...]) -> str:
    cleaned = text
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


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
