import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audi28.approval import ApprovalCenter
from audi28.cli import main
from audi28.doctor import doctor_report
from audi28.events import EventLog
from audi28.sessions import SessionStore
from audi28.supervisor import _record_loop_error
from tests.conftest import write_config

SECRET = "sk-test-secret-value"
WEBHOOK = "https://audi.example/hook?token=webhook-secret-value"
WEBHOOK_KEY = "webhook-key-should-not-leak"
DASHBOARD = "dashboard-token-should-not-leak"


def _pass_probes(**extra):
    probes = {
        "sdk_version": "0.2.2rc2",
        "dsh_available": True,
        "health_ok": True,
        "uv_version": "uv 0.0.0-test",
    }
    probes.update(extra)
    return probes


def _ready(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (home / "Code" / "deepseek-workspace").mkdir(parents=True)
    path = write_config(tmp_path)
    for relative in (
        "data/inbox/audi",
        "data/inbox/audi/processing",
        "data/inbox/audi/quarantine",
        "data/outbox/grokbot/pending",
        "data/outbox/grokbot/inflight",
        "data/outbox/grokbot/sent",
    ):
        (tmp_path / relative).mkdir(parents=True)
    env = {
        "DEEPSEEK_API_KEY": SECRET,
        "AUDI28_GROKBOT_WEBHOOK_URL": WEBHOOK,
        "AUDI28_GROKBOT_WEBHOOK_KEY": WEBHOOK_KEY,
        "AUDI28_DASHBOARD_TOKEN": DASHBOARD,
    }
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "supervisor.json").write_text(
        json.dumps({"pid": os.getpid(), "heartbeat": now, "port": 8728}),
        encoding="utf-8",
    )
    return path, env


def _names(report):
    return {item.name: item for item in report.checks}


def test_ready_environment_passes_without_printing_secrets(tmp_path, monkeypatch):
    path, env = _ready(tmp_path, monkeypatch)

    report = doctor_report(path, environ=env, probes=_pass_probes())

    assert report.exit_code == 0
    assert "FAIL" not in report.text
    for secret in (SECRET, WEBHOOK, WEBHOOK_KEY, DASHBOARD):
        assert secret not in report.text
    checks = _names(report)
    assert checks["DEEPSEEK_API_KEY"].status == "PASS"
    assert f"长度 {len(SECRET)}" in checks["DEEPSEEK_API_KEY"].detail
    assert checks["deepseek-sdk"].status == "PASS"
    assert checks["dsh"].status == "PASS"
    assert checks["workspace"].status == "PASS"
    assert checks["sandbox"].status == "INFO"
    assert "默认关闭" in checks["sandbox"].detail
    assert "WARN sandbox:" not in report.text
    assert "INFO sandbox:" in report.text
    assert "fix:" not in report.text.split("INFO sandbox:", 1)[1].split("\n", 1)[0]
    assert checks["sessions"].detail.startswith("0 ")


def test_missing_key_fails_with_a_fix_and_does_not_crash(tmp_path, monkeypatch):
    path, env = _ready(tmp_path, monkeypatch)
    env["DEEPSEEK_API_KEY"] = ""

    report = doctor_report(path, environ=env, probes=_pass_probes())

    assert report.exit_code == 1
    check = _names(report)["DEEPSEEK_API_KEY"]
    assert check.status == "FAIL"
    assert ".env" in check.fix
    assert "Traceback" not in report.text
    assert SECRET not in report.text


def test_warnings_do_not_fail_and_real_failures_do(tmp_path, monkeypatch):
    path, env = _ready(tmp_path, monkeypatch)
    env.pop("AUDI28_GROKBOT_WEBHOOK_URL")
    env.pop("AUDI28_GROKBOT_WEBHOOK_KEY")
    (tmp_path / "data" / "inbox" / "audi" / "quarantine" / "bad.json").write_text("{}", encoding="utf-8")

    warned = doctor_report(path, environ=env, probes=_pass_probes())

    assert warned.exit_code == 0
    checks = _names(warned)
    assert checks["AUDI28_GROKBOT_WEBHOOK_URL"].status == "WARN"
    assert "outbox" in checks["AUDI28_GROKBOT_WEBHOOK_URL"].detail
    assert checks["inbox"].status == "WARN"
    assert WEBHOOK not in warned.text

    for item in (tmp_path / "data" / "outbox" / "grokbot" / "pending").glob("*.json"):
        item.unlink()
    for index in range(10):
        (tmp_path / "data" / "outbox" / "grokbot" / "pending" / f"{index}.json").write_text("{}", encoding="utf-8")
    piled = doctor_report(path, environ=env, probes=_pass_probes())
    assert _names(piled)["outbox"].status == "WARN"
    assert piled.exit_code == 0

    (tmp_path / "home" / "Code" / "deepseek-workspace").rmdir()
    failed = doctor_report(path, environ=env, probes=_pass_probes())
    assert failed.exit_code == 1
    assert _names(failed)["workspace"].status == "FAIL"
    assert "mkdir -p" in _names(failed)["workspace"].fix


def test_sdk_uv_python_and_dsh_failures_name_the_fix(tmp_path, monkeypatch):
    path, env = _ready(tmp_path, monkeypatch)

    report = doctor_report(
        path,
        environ=env,
        probes=_pass_probes(
            python_info=(3, 9, 0),
            python_label="3.9.0",
            uv_version=None,
            sdk_version=None,
            dsh_available=False,
        ),
    )

    assert report.exit_code == 1
    checks = _names(report)
    assert checks["python"].status == "FAIL"
    assert checks["uv"].status == "FAIL"
    assert "uv/install.sh" in checks["uv"].fix
    assert checks["deepseek-sdk"].status == "FAIL"
    assert checks["deepseek-sdk"].fix == "uv sync --extra deepseek"
    assert checks["dsh"].fix == "uv sync --extra deepseek"

    mismatch = doctor_report(path, environ=env, probes=_pass_probes(sdk_version="0.0.1"))
    assert _names(mismatch)["deepseek-sdk"].status == "FAIL"
    assert "0.2.2rc2" in _names(mismatch)["deepseek-sdk"].detail


def test_service_health_and_counts(tmp_path, monkeypatch):
    path, env = _ready(tmp_path, monkeypatch)
    SessionStore(tmp_path / "data" / "sessions").create(
        title="跑测试",
        backend="deepseek",
        backend_session_id="session-1",
        workspace=str(tmp_path),
        task_id=None,
    )
    ApprovalCenter(tmp_path / "data" / "approvals").create(
        kind="approval",
        session_id="ses-1",
        backend_session_id="session-1",
        rpc_id="7",
        tool_name="shell",
        text="DeepSeek 在问你：可以吗",
        spoken="DeepSeek 在问你：可以吗",
    )
    EventLog(tmp_path / "data" / "events").append(
        type="error",
        source="supervisor",
        data={"reason": f"leaked {SECRET} and {DASHBOARD}"},
    )

    report = doctor_report(path, environ=env, probes=_pass_probes())

    checks = _names(report)
    assert checks["supervisor"].status == "PASS"
    assert checks["dashboard"].status == "PASS"
    assert checks["sessions"].detail.startswith("1 ")
    assert checks["approvals"].detail.startswith("1 ")
    assert checks["events"].status == "WARN"
    assert SECRET not in report.text
    assert DASHBOARD not in report.text
    assert "[redacted]" in checks["events"].detail

    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    (tmp_path / "data" / "supervisor.json").write_text(
        json.dumps({"pid": os.getpid(), "heartbeat": stale.isoformat(), "port": 8728}),
        encoding="utf-8",
    )
    stale_report = doctor_report(path, environ=env, probes=_pass_probes())
    assert _names(stale_report)["supervisor"].status == "WARN"

    (tmp_path / "data" / "supervisor.json").write_text(
        json.dumps({"pid": 2_147_483_647, "heartbeat": datetime.now().astimezone().isoformat(), "port": 8728}),
        encoding="utf-8",
    )
    down = doctor_report(path, environ=env, probes=_pass_probes(health_ok=False))
    assert _names(down)["supervisor"].status == "WARN"
    assert _names(down)["dashboard"].status == "WARN"
    assert down.exit_code == 0

    (tmp_path / "data" / "supervisor.json").write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "heartbeat": datetime.now().astimezone().isoformat(timespec="seconds"),
                "port": 8728,
            }
        ),
        encoding="utf-8",
    )
    sick = doctor_report(path, environ=env, probes=_pass_probes(health_ok=False))
    assert _names(sick)["dashboard"].status == "FAIL"
    assert sick.exit_code == 1


def test_missing_directory_is_a_mkdir_fix(tmp_path, monkeypatch):
    path, env = _ready(tmp_path, monkeypatch)
    (tmp_path / "data" / "inbox" / "audi" / "processing").rmdir()

    report = doctor_report(path, environ=env, probes=_pass_probes())

    assert report.exit_code == 1
    check = _names(report)["inbox"]
    assert check.status == "FAIL"
    assert check.fix.startswith("mkdir -p ")
    assert "processing" in check.fix


def test_json_output_redacts_secrets(tmp_path, monkeypatch):
    path, env = _ready(tmp_path, monkeypatch)
    EventLog(tmp_path / "data" / "events").append(
        type="error",
        source="supervisor",
        data={"reason": WEBHOOK},
    )

    report = doctor_report(path, environ=env, probes=_pass_probes(), as_json=True)

    payload = json.loads(report.text)
    assert payload["ok"] is True
    assert any(item["name"] == "events" and item["status"] == "WARN" for item in payload["checks"])
    assert WEBHOOK not in report.text
    assert SECRET not in report.text


def test_mock_deepseek_answers_without_a_network_client():
    path = Path(__file__).resolve().parents[1] / "scripts" / "mock_deepseek.py"
    spec = importlib.util.spec_from_file_location("mock_deepseek", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.MockHarness().start_session("session-1").run("跑测试").final_response
    assert "假的 DeepSeek" in answer
    assert "sk-" not in answer


def test_bad_config_and_missing_file_do_not_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    missing = doctor_report(tmp_path / "config.toml", environ={}, probes=_pass_probes())
    assert missing.exit_code == 1
    assert "config.example.toml" in missing.text
    assert "Traceback" not in missing.text

    (tmp_path / "config.toml").write_text("[nope]\nx = 1\n", encoding="utf-8")
    bad = doctor_report(tmp_path / "config.toml", environ={"DEEPSEEK_API_KEY": SECRET}, probes=_pass_probes())
    assert bad.exit_code == 1
    assert "nope" in bad.text
    assert SECRET not in bad.text
    assert "Traceback" not in bad.text


def test_doctor_command_json_and_missing_key(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("AUDI28_DASHBOARD_TOKEN", raising=False)
    monkeypatch.delenv("AUDI28_GROKBOT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AUDI28_GROKBOT_WEBHOOK_KEY", raising=False)
    path = write_config(tmp_path)

    code = main(["--config", str(path), "doctor", "--json"])

    text = capsys.readouterr().out
    payload = json.loads(text)
    assert code == 1
    assert any(item["name"] == "DEEPSEEK_API_KEY" and item["status"] == "FAIL" for item in payload["checks"])
    assert "Traceback" not in text


def test_loop_error_redacts_the_dashboard_token(tmp_path):
    events = EventLog(tmp_path / "events")
    _record_loop_error(
        events,
        RuntimeError(f"header {DASHBOARD} url {WEBHOOK}"),
        secrets=(WEBHOOK, "webhook-key", DASHBOARD),
    )
    stored = events.replay()[-1].data["detail"]
    assert DASHBOARD not in stored
    assert WEBHOOK not in stored
    assert "[redacted]" in stored
