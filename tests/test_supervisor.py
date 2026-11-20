import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from audi28.config import load_config
from audi28.events import EventLog
from audi28.supervisor import _join_workers, _sync_dependencies, restart_argv, supervise
from tests.conftest import write_config

SECRET = "sk-restart-secret"


def _wait_health(port: int) -> dict:
    deadline = time.time() + 3
    last_error: Exception | None = None
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                assert response.status == 200
                return json.loads(response.read().decode())
        except (urllib.error.URLError, AssertionError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.02)
    raise AssertionError(f"health check failed: {last_error}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start(tmp_path: Path, monkeypatch, *, reexec, sync_runner=None, rewrite=None):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    monkeypatch.setenv("AUDI28_RESTARTS", "2")
    port = _free_port()
    path = write_config(tmp_path, port=port)
    if rewrite is not None:
        path.write_text(rewrite(path.read_text(encoding="utf-8")), encoding="utf-8")
    config = load_config(path)
    stop = threading.Event()
    thread = threading.Thread(
        target=lambda: supervise(
            config,
            stop=stop,
            reexec=reexec,
            sync_runner=sync_runner,
            config_path=path,
        ),
        daemon=True,
    )
    thread.start()
    _wait_health(port)
    return config, stop, thread, port


def test_stale_restart_request_is_dropped_and_a_normal_stop_does_not_reexec(tmp_path, monkeypatch):
    calls: list[str] = []
    trigger = tmp_path / "data" / "restart.request"
    trigger.parent.mkdir(parents=True)
    trigger.write_text("leftover\n", encoding="utf-8")

    _config, stop, thread, port = _start(tmp_path, monkeypatch, reexec=lambda _argv: calls.append("reexec"))
    try:
        assert not trigger.exists()
        assert calls == []
        state = json.loads((tmp_path / "data" / "supervisor.json").read_text(encoding="utf-8"))
        assert state["restarts"] == 2
        assert _wait_health(port)["ok"] is True
    finally:
        stop.set()
        thread.join(timeout=3)

    assert thread.is_alive() is False
    assert calls == []


def test_restart_request_reexecs_and_syncs_when_pyproject_changes(tmp_path, monkeypatch):
    seen: dict[str, str] = {}
    synced: list[tuple[list[str], Path]] = []
    project = tmp_path / "pyproject.toml"
    project.write_text("[project]\nname = 'before'\n", encoding="utf-8")

    def reexec(argv: list[str]) -> None:
        seen["argv"] = " ".join(argv)
        seen["restarts"] = os.environ.get("AUDI28_RESTARTS", "")
        seen["reason"] = os.environ.get("AUDI28_RESTART_REASON", "")

    config, _stop, thread, port = _start(
        tmp_path,
        monkeypatch,
        reexec=reexec,
        sync_runner=lambda command, cwd: synced.append((command, cwd)),
    )
    project.write_text("[project]\nname = 'after'\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(project, (later, later))
    reason = f"更新代码 {SECRET} " + ("x" * 300)
    (config.root / "data" / "restart.request").write_text(reason, encoding="utf-8")
    thread.join(timeout=5)

    assert thread.is_alive() is False
    assert seen["restarts"] == "3"
    assert seen["argv"] == " ".join(restart_argv(config.root / "config.toml"))
    assert seen["argv"].split()[0] == sys.executable
    assert SECRET not in seen["reason"]
    assert seen["reason"].startswith("更新代码 [redacted]")
    assert len(seen["reason"]) <= 200
    assert synced == [(["uv", "sync", "--extra", "deepseek"], config.root)]
    assert "--extra" in synced[0][0]
    assert "dev" not in synced[0][0]
    assert not (config.root / "data" / "restart.request").exists()
    assert not (config.root / "data" / "supervisor.json").exists()
    events = EventLog(config.root / "data" / "events").replay()
    recorded = [event for event in events if event.type == "supervisor"]
    assert recorded[-1].data["action"] == "restart"
    assert SECRET not in recorded[-1].data["reason"]
    with socket.socket() as sock:
        sock.settimeout(0.5)
        assert sock.connect_ex(("127.0.0.1", port)) != 0


def test_restart_without_a_pyproject_change_skips_uv_sync(tmp_path, monkeypatch):
    synced: list[list[str]] = []
    project = tmp_path / "pyproject.toml"
    project.write_text("[project]\nname = 'same'\n", encoding="utf-8")

    _config, _stop, thread, _port = _start(
        tmp_path,
        monkeypatch,
        reexec=lambda _argv: None,
        sync_runner=lambda command, cwd: synced.append(command),
    )
    (tmp_path / "data" / "restart.request").write_text("接着跑\n", encoding="utf-8")
    thread.join(timeout=5)

    assert thread.is_alive() is False
    assert synced == []


def test_disabled_deepseek_sync_omits_the_extra(tmp_path, monkeypatch):
    synced: list[list[str]] = []
    project = tmp_path / "pyproject.toml"
    project.write_text("[project]\nname = 'before'\n", encoding="utf-8")

    def disable(text: str) -> str:
        return text.replace("[deepseek]\nenabled = true\n", "[deepseek]\nenabled = false\n", 1)

    config, _stop, thread, _port = _start(
        tmp_path,
        monkeypatch,
        reexec=lambda _argv: None,
        sync_runner=lambda command, cwd: synced.append(command),
        rewrite=disable,
    )
    assert config.deepseek.enabled is False
    project.write_text("[project]\nname = 'after'\n", encoding="utf-8")
    later = time.time() + 5
    os.utime(project, (later, later))
    (tmp_path / "data" / "restart.request").write_text("关掉 extra\n", encoding="utf-8")
    thread.join(timeout=5)

    assert thread.is_alive() is False
    assert synced == [["uv", "sync"]]


def test_missing_uv_and_failed_sync_are_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(write_config(tmp_path))
    (tmp_path / "pyproject.toml").write_text("name = 'x'\n", encoding="utf-8")
    events = EventLog(tmp_path / "events")
    monkeypatch.setattr("audi28.supervisor.shutil.which", lambda _name: None)

    _sync_dependencies(config, 0.0, None, events)

    monkeypatch.setattr("audi28.supervisor.shutil.which", lambda _name: "/usr/bin/uv")

    def failed(command, cwd, check=False):
        del cwd, check
        return subprocess.CompletedProcess(command, 7)

    monkeypatch.setattr("audi28.supervisor.subprocess.run", failed)
    _sync_dependencies(config, 0.0, None, events)

    reasons = [event.data["reason"] for event in events.replay() if event.type == "error"]
    assert any("找不到 uv" in reason for reason in reasons)
    assert any("退出码 7" in reason for reason in reasons)


def test_join_timeout_records_the_threads_that_are_still_alive(tmp_path):
    events = EventLog(tmp_path / "events")
    hold = threading.Event()
    web = threading.Thread(target=hold.wait, daemon=True)
    transport = threading.Thread(target=hold.wait, daemon=True)
    web.start()
    transport.start()

    _join_workers(web, transport, events, timeout=0.05)

    reasons = [event.data["reason"] for event in events.replay() if event.type == "error"]
    assert len(reasons) == 1
    assert "web" in reasons[0]
    assert "transport" in reasons[0]
    hold.set()
    web.join(timeout=1)
    transport.join(timeout=1)
