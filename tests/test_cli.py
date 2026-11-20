import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from audi28.cli import main
from audi28.events import EventLog
from tests.conftest import ROOT, write_config


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_health(port: int) -> None:
    deadline = time.time() + 20
    url = f"http://127.0.0.1:{port}/health"
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                body = json.loads(response.read().decode())
            assert body["ok"] is True
            return
        except Exception as exc:  # noqa: BLE001 - retry until the server is up
            last_error = exc
            time.sleep(0.05)
    raise AssertionError(f"start.sh did not serve /health: {last_error}")


def test_events_replay_prints_unknown_type(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = write_config(tmp_path)
    log = EventLog(tmp_path / "data" / "events")
    log.append(type="made-up-type", source="cli", data={"ok": True})

    code = main(["--config", str(path), "events", "replay"])

    assert code == 0
    assert "made-up-type" in capsys.readouterr().out


def test_doctor_command_reports_missing_key(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = write_config(tmp_path)

    code = main(["--config", str(path), "doctor"])

    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert code == 1
    assert "DEEPSEEK_API_KEY" in text
    assert "Traceback" not in text


def test_gitignore_covers_secrets_data_and_venv():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    lines = {line.strip() for line in text.splitlines()}
    assert ".env" in lines
    assert "data/" in lines
    assert ".venv/" in lines or ".venv" in lines


def test_ci_workflow_runs_pytest():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "pytest" in text
    assert "uv sync" in text


def test_start_sh_invokes_supervise():
    text = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "audi28 supervise" in text
    assert "--extra deepseek" in text
    assert os.access(ROOT / "start.sh", os.X_OK)


def _stop_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        proc.terminate()


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="GitHub macOS runners time out connecting to the start.sh child; Linux CI covers it.",
)
def test_start_sh_serves_health(tmp_path):
    port = _free_port()
    path = write_config(tmp_path, port=port)
    path.write_text(path.read_text(encoding="utf-8").replace("enabled = true", "enabled = false", 1), encoding="utf-8")
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env.pop("DEEPSEEK_API_KEY", None)
    proc = subprocess.Popen(
        [str(ROOT / "start.sh"), "--config", str(path)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    error: AssertionError | None = None
    try:
        _wait_health(port)
    except AssertionError as exc:
        error = exc
    finally:
        _stop_process_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        output = proc.stdout.read() if proc.stdout is not None else ""
    if error is not None:
        raise AssertionError(f"{error}\n{output}")
    assert "Traceback" not in output
    assert Path(ROOT / "start.sh").exists()
