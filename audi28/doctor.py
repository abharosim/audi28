"""Report whether audi28 can start. Never prints secret values."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from audi28.approval import ApprovalCenter
from audi28.config import Config, ConfigError, load_config, load_dotenv
from audi28.events import EventLog
from audi28.sandbox import render_profile, sandbox_exec_path, self_test
from audi28.sessions import SessionStore

_SDK_NAME = "deepseek-harness-sdk"
_PENDING_HIGH = 10
_HEARTBEAT_STALE_S = 30
_OPEN = {"open", "idle", "running"}
_PIN_RE = re.compile(r"deepseek-harness-sdk==([^\s\"']+)")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str

    def line(self) -> str:
        if self.status in {"PASS", "INFO"}:
            return f"{self.status} {self.name}: {self.detail}"
        return f"{self.status} {self.name}: {self.detail} fix: {self.fix}"

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass(frozen=True)
class DoctorReport:
    text: str
    exit_code: int
    checks: tuple[Check, ...] = ()


def doctor_report(
    config_path: Path,
    environ: Mapping[str, str] | None = None,
    *,
    as_json: bool = False,
    probes: Mapping[str, Any] | None = None,
) -> DoctorReport:
    """Run the doctor checks. A missing key is a FAIL line, not a crash."""

    env: dict[str, str] = dict(os.environ if environ is None else environ)
    dotenv_path = config_path.parent / ".env"
    load_dotenv(dotenv_path, env)
    probe = dict(probes or {})
    secrets = _secrets(env)
    checks: list[Check] = [
        _python_check(probe),
        _uv_check(probe),
    ]
    config, config_check = _config_check(config_path)
    checks.append(config_check)
    pin = _pinned_sdk()
    checks.append(_sdk_check(pin, probe))
    checks.append(_dsh_check(probe))
    checks.append(_api_key_check(env, dotenv_path))
    checks.extend(_webhook_checks(env, dotenv_path))
    checks.append(_dashboard_token_check(env, dotenv_path))
    if config is None:
        checks.append(
            Check(
                "dirs",
                "FAIL",
                "配置没加载，目录还没法查。",
                config_check.fix,
            )
        )
    else:
        checks.extend(_dir_checks(config))
        checks.append(_workspace_check(config))
        checks.append(_sandbox_check(config, env))
        checks.extend(_service_checks(config, probe))
        checks.append(_approvals_check(config))
        checks.append(_sessions_check(config))
        checks.append(_last_error_check(config, secrets))
    failed = any(item.status == "FAIL" for item in checks)
    text = _render(checks, as_json=as_json, secrets=secrets)
    return DoctorReport(text=text, exit_code=1 if failed else 0, checks=tuple(checks))


def _render(checks: list[Check], *, as_json: bool, secrets: list[str]) -> str:
    if as_json:
        body = json.dumps(
            {"ok": all(item.status != "FAIL" for item in checks), "checks": [item.as_dict() for item in checks]},
            ensure_ascii=False,
            indent=2,
        )
        return _redact(body, secrets) + "\n"
    return _redact("\n".join(item.line() for item in checks) + "\n", secrets)


def _python_check(probe: Mapping[str, Any]) -> Check:
    info = probe.get("python_info", sys.version_info)
    label = str(probe.get("python_label", platform.python_version()))
    if tuple(info[:2]) >= (3, 10):
        return Check("python", "PASS", label, "")
    return Check("python", "FAIL", f"{label} 太旧。", "安装 Python 3.10 或更新的版本。")


def _uv_check(probe: Mapping[str, Any]) -> Check:
    if "uv_version" in probe:
        version = probe["uv_version"]
    else:
        version = _uv_version()
    if not version:
        return Check(
            "uv",
            "FAIL",
            "找不到 uv。",
            "curl -LsSf https://astral.sh/uv/install.sh | sh",
        )
    return Check("uv", "PASS", str(version), "")


def _uv_version() -> str | None:
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        completed = subprocess.run(
            [uv, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    text = (completed.stdout or completed.stderr).strip()
    return text or None


def _config_check(path: Path) -> tuple[Config | None, Check]:
    if not path.is_file():
        return None, Check(
            "config",
            "FAIL",
            f"找不到 {path}。",
            "cp config.example.toml config.toml",
        )
    try:
        config = load_config(path)
    except ConfigError as exc:
        return None, Check("config", "FAIL", str(exc), "按报错改 config.toml。未知的键会被拒绝。")
    return config, Check("config", "PASS", f"{path.name} 可以加载，未知键会被拒绝。", "")


def _sdk_check(pin: str, probe: Mapping[str, Any]) -> Check:
    if "sdk_version" in probe:
        version = probe["sdk_version"]
    else:
        version = _installed_sdk()
    if not version:
        return Check("deepseek-sdk", "FAIL", f"没装 {_SDK_NAME}。", "uv sync --extra deepseek")
    if version != pin:
        return Check(
            "deepseek-sdk",
            "FAIL",
            f"装的是 {version}，钉的是 {pin}。",
            "uv sync --extra deepseek",
        )
    return Check("deepseek-sdk", "PASS", f"{_SDK_NAME} {version}", "")


def _installed_sdk() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:
        return None
    try:
        return version(_SDK_NAME)
    except PackageNotFoundError:
        return None


def _dsh_check(probe: Mapping[str, Any]) -> Check:
    if "dsh_available" in probe:
        available = bool(probe["dsh_available"])
    else:
        available = _dsh_available()
    if available:
        return Check("dsh", "PASS", "捆绑的 dsh 运行时可用。", "")
    return Check(
        "dsh",
        "FAIL",
        "找不到 DeepSeek 运行时里的 dsh。",
        "uv sync --extra deepseek",
    )


def _dsh_available() -> bool:
    try:
        from deepseek_harness_runtime import resolve_bundled_launch_args
    except ImportError:
        return False
    try:
        launch = resolve_bundled_launch_args()
    except Exception:
        return False
    return bool(launch)


def _api_key_check(env: Mapping[str, str], dotenv_path: Path) -> Check:
    value = env.get("DEEPSEEK_API_KEY", "").strip()
    if not value:
        return Check(
            "DEEPSEEK_API_KEY",
            "FAIL",
            "没有设置。",
            f"把 DEEPSEEK_API_KEY 写进 {dotenv_path}，或导出到环境变量。不要把值写进 config.toml。",
        )
    return Check("DEEPSEEK_API_KEY", "PASS", f"已设置，长度 {len(value)}。", "")


def _webhook_checks(env: Mapping[str, str], dotenv_path: Path) -> list[Check]:
    url = env.get("AUDI28_GROKBOT_WEBHOOK_URL", "").strip()
    key = env.get("AUDI28_GROKBOT_WEBHOOK_KEY", "").strip()
    fix = f"把 AUDI28_GROKBOT_WEBHOOK_URL 和 AUDI28_GROKBOT_WEBHOOK_KEY 写进 {dotenv_path}。"
    checks: list[Check] = []
    if url:
        checks.append(Check("AUDI28_GROKBOT_WEBHOOK_URL", "PASS", "已设置。", ""))
    else:
        checks.append(
            Check(
                "AUDI28_GROKBOT_WEBHOOK_URL",
                "WARN",
                "没有设置。发给 Audi 的消息会留在 outbox 里。",
                fix,
            )
        )
    if key:
        checks.append(Check("AUDI28_GROKBOT_WEBHOOK_KEY", "PASS", f"已设置，长度 {len(key)}。", ""))
    else:
        checks.append(
            Check(
                "AUDI28_GROKBOT_WEBHOOK_KEY",
                "WARN",
                "没有设置。发给 Audi 的消息会留在 outbox 里。",
                fix,
            )
        )
    return checks


def _dashboard_token_check(env: Mapping[str, str], dotenv_path: Path) -> Check:
    value = env.get("AUDI28_DASHBOARD_TOKEN", "").strip()
    if not value:
        return Check(
            "AUDI28_DASHBOARD_TOKEN",
            "FAIL",
            "没有设置。仪表盘除 /health 外会拒绝访问。",
            f"把 AUDI28_DASHBOARD_TOKEN 写进 {dotenv_path}。",
        )
    return Check("AUDI28_DASHBOARD_TOKEN", "PASS", f"已设置，长度 {len(value)}。", "")


def _workspace_check(config: Config) -> Check:
    path = config.deepseek.workspace
    if not path.exists():
        return Check("workspace", "FAIL", f"{path} 不存在。", f"mkdir -p {path}")
    if not path.is_dir() or not os.access(path, os.W_OK):
        return Check("workspace", "FAIL", f"{path} 不可写。", f"chmod u+w {path}")
    return Check("workspace", "PASS", f"{path} 存在且可写。", "")


def _sandbox_check(config: Config, env: Mapping[str, str]) -> Check:
    if not config.deepseek.sandbox.enabled:
        return Check(
            "sandbox",
            "INFO",
            "沙箱默认关闭。要启用，把 [deepseek.sandbox] enabled 设为 true。",
            "",
        )
    binary = sandbox_exec_path()
    profile = config.root / "data" / "sandbox" / "deepseek.sb"
    if binary is None:
        return Check(
            "sandbox",
            "WARN",
            "配置开了沙箱，但这台系统没有 sandbox-exec，运行时不会被套住。",
            "在 macOS 上跑。没有 sandbox-exec 时只记 WARN，继续启动。",
        )
    home = Path(env.get("HOME") or str(Path.home())).expanduser()
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        render_profile(
            home=home,
            workspace=config.deepseek.workspace,
            dsh_home=config.deepseek.dsh_home,
            project_root=config.root,
            extra_read_paths=config.deepseek.sandbox.extra_read_paths,
            sandbox_dir=profile.parent,
        ),
        encoding="utf-8",
    )
    canary = home / f".audi28-sandbox-canary-{os.getpid()}"
    sample = config.deepseek.workspace / f".audi28-sandbox-ok-{os.getpid()}"
    try:
        canary.write_text("audi28-canary\n", encoding="utf-8")
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text("workspace-ok\n", encoding="utf-8")
        denied, allowed = self_test(profile, canary, sample)
    except OSError as exc:
        return Check(
            "sandbox",
            "FAIL",
            f"自测没能准备临时文件：{exc.__class__.__name__}。profile 在 {profile}。",
            "确认家目录和工作区可写。",
        )
    finally:
        canary.unlink(missing_ok=True)
        sample.unlink(missing_ok=True)
    if denied and allowed:
        return Check(
            "sandbox",
            "PASS",
            f"沙箱生效。profile 在 {profile}。家目录探针被拒绝，工作区文件可以读。",
            "",
        )
    return Check(
        "sandbox",
        "FAIL",
        f"自测没通过（家目录拒绝={denied}，工作区可读={allowed}）。profile 在 {profile}。",
        "检查 profile 里家目录的 deny 在工作区 allow 之前。",
    )


def _dir_checks(config: Config) -> list[Check]:
    root = config.root
    inbox = root / "data" / "inbox" / "audi"
    outbox = root / "data" / "outbox" / "grokbot"
    inbox_dirs = {
        "inbox": inbox,
        "processing": inbox / "processing",
        "quarantine": inbox / "quarantine",
    }
    outbox_dirs = {
        "pending": outbox / "pending",
        "inflight": outbox / "inflight",
        "sent": outbox / "sent",
    }
    return [
        _tree_check("inbox", inbox_dirs, warn_name="quarantine"),
        _tree_check("outbox", outbox_dirs, warn_name="pending", warn_at=_PENDING_HIGH),
    ]


def _tree_check(
    name: str,
    dirs: dict[str, Path],
    *,
    warn_name: str,
    warn_at: int = 1,
) -> Check:
    missing = [path for path in dirs.values() if not path.is_dir()]
    if missing:
        command = "mkdir -p " + " ".join(str(path) for path in dirs.values())
        return Check(name, "FAIL", "缺少 " + ", ".join(str(path) for path in missing) + "。", command)
    blocked = [path for path in dirs.values() if not os.access(path, os.W_OK)]
    if blocked:
        return Check(name, "FAIL", "不可写 " + ", ".join(str(path) for path in blocked) + "。", "chmod u+w " + " ".join(str(path) for path in blocked))
    counts = {label: _file_count(path) for label, path in dirs.items()}
    detail = "，".join(f"{label} {count}" for label, count in counts.items())
    flagged = counts.get(warn_name, 0)
    if warn_name == "quarantine" and flagged:
        return Check(name, "WARN", f"{detail}。quarantine 里有文件。", "看 quarantine 里的文件，修好后移走。")
    if warn_name == "pending" and flagged >= warn_at:
        return Check(
            name,
            "WARN",
            f"{detail}。pending 有 {flagged} 个，积压偏高。",
            "确认 webhook 可达。消息会留在 outbox，不会丢。",
        )
    return Check(name, "PASS", f"{detail}。", "")


def _file_count(path: Path) -> int:
    return sum(1 for item in path.iterdir() if item.is_file() and not item.name.startswith("."))


def _service_checks(config: Config, probe: Mapping[str, Any]) -> list[Check]:
    state = _read_supervisor(config.root / "data" / "supervisor.json")
    running = bool(state and _pid_alive(int(state["pid"])))
    fresh = bool(state and _heartbeat_fresh(str(state.get("heartbeat") or "")))
    port = int(state["port"]) if state and str(state.get("port") or "").isdigit() else config.web.port
    if not running:
        supervisor = Check("supervisor", "WARN", "服务没在跑。", "./start.sh")
    elif not fresh:
        supervisor = Check("supervisor", "WARN", f"pid {state['pid']} 的心跳停了。", "./start.sh")
    else:
        supervisor = Check("supervisor", "PASS", f"pid {state['pid']}，心跳正常。", "")
    url = f"http://{config.web.host}:{port}/health"
    if "health_ok" in probe:
        healthy = bool(probe["health_ok"])
    else:
        healthy = _health_ok(url)
    if healthy:
        dashboard = Check("dashboard", "PASS", f"{url} 返回 ok。", "")
    elif not running:
        dashboard = Check("dashboard", "WARN", f"{url} 没有响应，因为服务没在跑。", "./start.sh")
    else:
        dashboard = Check("dashboard", "FAIL", f"进程在，但 {url} 没有响应。", "./start.sh")
    return [supervisor, dashboard]


def _read_supervisor(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict) or not str(obj.get("pid") or "").isdigit():
        return None
    return obj


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _heartbeat_fresh(stamp: str) -> bool:
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if moment.tzinfo is None:
        return False
    return (datetime.now().astimezone() - moment).total_seconds() <= _HEARTBEAT_STALE_S


def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=0.5) as response:
            if response.status != 200:
                return False
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return False
    return isinstance(body, dict) and body.get("ok") is True


def _approvals_check(config: Config) -> Check:
    pending = ApprovalCenter(config.root / "data" / "approvals").list_pending()
    if not pending:
        return Check("approvals", "PASS", "0 条待处理。", "")
    oldest = min(pending, key=lambda record: record.created_ts)
    age = _age(oldest.created_ts)
    return Check("approvals", "PASS", f"{len(pending)} 条待处理，最老的已等 {age}。", "")


def _sessions_check(config: Config) -> Check:
    records = SessionStore(config.root / "data" / "sessions").list(include_closed=True)
    count = sum(1 for record in records if record.status in _OPEN)
    return Check("sessions", "PASS", f"{count} 个仍开放。", "")


def _last_error_check(config: Config, secrets: list[str]) -> Check:
    try:
        events = EventLog(config.root / "data" / "events").replay()
    except Exception:
        return Check("events", "PASS", "没有 error 事件。", "")
    errors = [event for event in events if event.type == "error"]
    if not errors:
        return Check("events", "PASS", "没有 error 事件。", "")
    last = errors[-1]
    reason = ""
    if isinstance(last.data, dict):
        reason = str(last.data.get("reason") or last.data.get("detail") or "")
    reason = _redact(reason, secrets)
    return Check(
        "events",
        "WARN",
        f"最近一条 error 在 {last.ts}：{reason or '没有原因'}。",
        f"打开 {config.root / 'data' / 'events'} 看完整记录。",
    )


def _age(stamp: str) -> str:
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return "未知时间"
    if moment.tzinfo is None:
        return "未知时间"
    seconds = max(0, int((datetime.now().astimezone() - moment).total_seconds()))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def _pinned_sdk() -> str:
    path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return "0.2.2rc2"
    match = _PIN_RE.search(text)
    return match.group(1) if match else "0.2.2rc2"


def _secrets(env: Mapping[str, str]) -> list[str]:
    names = (
        "DEEPSEEK_API_KEY",
        "AUDI28_GROKBOT_WEBHOOK_URL",
        "AUDI28_GROKBOT_WEBHOOK_KEY",
        "AUDI28_DASHBOARD_TOKEN",
    )
    return [env.get(name, "").strip() for name in names if env.get(name, "").strip()]


def _redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted
