from pathlib import Path

import sys

import pytest

from audi28.config import ConfigError, load_config, load_dotenv
from tests.conftest import EXAMPLE, write_config


def test_example_config_loads(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    path = write_config(tmp_path / "proj")

    cfg = load_config(path)

    assert cfg.root == path.parent
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 8728
    assert cfg.deepseek.workspace == home / "Code" / "deepseek-workspace"
    assert cfg.deepseek.workspace.is_absolute()
    assert cfg.deepseek.permission_mode == "workspace-write"
    assert cfg.deepseek.approval_requests is True
    assert cfg.deepseek.dsh_home == (path.parent / "data" / "dsh-home").resolve()
    assert cfg.deepseek.sandbox.enabled is (sys.platform == "darwin")
    assert cfg.deepseek.sandbox.extra_read_paths == ()
    assert cfg.route.llm is False
    assert cfg.route.default == "deepseek"
    assert cfg.route.new_task_phrases == ("新任务", "开一个新的", "另外一个任务", "new task")
    assert cfg.route.not_new_task_phrases == ("不是新任务", "接着上一个")
    assert "你直接回答" in cfg.route.self_phrases
    assert cfg.sessions.idle_timeout_s == 86400
    assert cfg.approval.remind_after_s is None
    assert "timeout_s" not in cfg.approval.__dict__
    assert "DEEPSEEK_API_KEY" not in cfg.__dict__


def test_unknown_section_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8") + "\n[not-a-section]\nfoo = 1\n"
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="未知 section「not-a-section」"):
        load_config(path)


def test_sandbox_extra_read_path_must_be_absolute(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "# extra_read_paths = [\"/opt/local/share\"]\n",
        'extra_read_paths = ["relative/path"]\n',
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="extra_read_paths"):
        load_config(path)


def test_unknown_sandbox_key_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "[deepseek.sandbox]\n",
        "[deepseek.sandbox]\nmode = \"loose\"\n",
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=r"deepseek\.sandbox\.mode"):
        load_config(path)


def test_approval_timeout_key_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "[approval]\n",
        "[approval]\ntimeout_s = 600\n",
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match=r"未知配置项 approval\.timeout_s"):
        load_config(path)


def test_secret_in_toml_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "approval_requests = true\n",
        'approval_requests = true\napi_key = "sk-nope"\n',
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="api_key"):
        load_config(path)


def test_relative_workspace_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        'workspace = "~/Code/deepseek-workspace"',
        'workspace = "deepseek-workspace"',
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="绝对路径"):
        load_config(path)


def test_non_loopback_host_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        'host = "127.0.0.1"',
        'host = "0.0.0.0"',
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="127.0.0.1"):
        load_config(path)


def test_missing_section_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    lines = [
        line
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
        if line.strip() != "[events]"
    ]
    path = tmp_path / "config.toml"
    path.write_text("".join(lines), encoding="utf-8")

    with pytest.raises(ConfigError, match="缺少 section「events」"):
        load_config(path)


def test_missing_config_file(tmp_path):
    with pytest.raises(ConfigError, match="找不到配置文件"):
        load_config(tmp_path / "config.toml")


def test_dotenv_fills_missing_and_does_not_override(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        'DEEPSEEK_API_KEY=from-file\nAUDI28_GROKBOT_WEBHOOK_KEY="abc"\n# comment\n\n',
        encoding="utf-8",
    )
    env = {"DEEPSEEK_API_KEY": "from-env"}

    load_dotenv(path, env)

    assert env["DEEPSEEK_API_KEY"] == "from-env"
    assert env["AUDI28_GROKBOT_WEBHOOK_KEY"] == "abc"


def test_missing_dotenv_is_fine(tmp_path):
    env: dict[str, str] = {}

    load_dotenv(tmp_path / ".env", env)

    assert env == {}


def test_danger_full_access_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        'permission_mode = "workspace-write"',
        'permission_mode = "danger-full-access"',
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigError, match="danger-full-access"):
        load_config(path)


def test_read_only_permission_mode_is_allowed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        'permission_mode = "workspace-write"',
        'permission_mode = "read-only"',
        1,
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")

    cfg = load_config(path)

    assert cfg.deepseek.permission_mode == "read-only"


def test_example_does_not_advertise_an_unknown_hooks_path():
    text = EXAMPLE.read_text(encoding="utf-8")
    assert 'path = "/approve"' not in text


def test_load_config_does_not_require_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = write_config(tmp_path)

    cfg = load_config(path)

    assert cfg.deepseek.enabled is True
