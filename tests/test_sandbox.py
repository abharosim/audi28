import sys
from pathlib import Path

import pytest

from audi28.sandbox import render_profile, sandbox_exec_path, self_test


def _profile(**overrides: object) -> str:
    home = Path("/Users/rowang")
    project = home / "Code" / "audi28"
    fields = {
        "home": home,
        "workspace": home / "Code" / "deepseek-workspace",
        "dsh_home": project / "data" / "dsh-home",
        "project_root": project,
        "extra_read_paths": (),
        "sandbox_dir": project / "data" / "sandbox",
        "temp_paths": (Path("/private/tmp"),),
    }
    fields.update(overrides)
    return render_profile(**fields)  # type: ignore[arg-type]


def test_profile_denies_home_and_project_env_but_allows_workspace(tmp_path):
    home = Path("/Users/rowang")
    project = home / "Code" / "audi28"
    workspace = home / "Code" / "deepseek-workspace"
    dsh_home = project / "data" / "dsh-home"
    quoted = Path('/Users/rowan"g/Code/extra')
    text = _profile(extra_read_paths=(quoted,))

    home_deny = text.index('(deny file-read* (subpath "/Users/rowang"))')
    workspace_allow = text.index(
        '(allow file-read* file-write* (subpath "/Users/rowang/Code/deepseek-workspace"))'
    )
    dsh_allow = text.index(
        '(allow file-read* file-write* (subpath "/Users/rowang/Code/audi28/data/dsh-home"))'
    )
    env_deny = text.index('(deny file-read* file-write* (literal "/Users/rowang/Code/audi28/.env"))')
    assert text.index('(subpath "/private/var/folders")') < home_deny
    assert home_deny < workspace_allow < env_deny
    assert home_deny < dsh_allow < env_deny
    assert '(allow default)' in text
    assert '(subpath "/Users/rowang/Code/audi28")' not in text
    assert '(allow file-read* (subpath "/Users/rowang/Code/audi28/.venv"))' in text
    assert '(allow file-read* file-write* (subpath "/private/tmp"))' in text
    assert '(allow file-read* (subpath "/usr"))' in text
    assert r"/Users/rowan\"g/Code/extra" in text
    assert "DEEPSEEK_API_KEY" not in text
    assert workspace.as_posix() in text
    assert dsh_home.as_posix() in text
    del project


@pytest.mark.skipif(
    sys.platform != "darwin" or sandbox_exec_path() is None,
    reason="needs macOS sandbox-exec",
)
def test_sandbox_exec_denies_a_home_canary_and_allows_the_workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    home.mkdir()
    project = home / "Code" / "audi28"
    workspace = home / "Code" / "deepseek-workspace"
    dsh_home = project / "data" / "dsh-home"
    dsh_home.mkdir(parents=True)
    workspace.mkdir(parents=True)
    (project / ".env").write_text("DEEPSEEK_API_KEY=sk-not-for-the-runtime\n", encoding="utf-8")
    canary = home / "audi28-canary.txt"
    canary.write_text("canary-secret\n", encoding="utf-8")
    visible = workspace / "note.txt"
    visible.write_text("workspace-ok\n", encoding="utf-8")
    profile = tmp_path / "deepseek.sb"
    profile.write_text(
        render_profile(
            home=home,
            workspace=workspace,
            dsh_home=dsh_home,
            project_root=project,
            sandbox_dir=project / "data" / "sandbox",
        ),
        encoding="utf-8",
    )

    denied, allowed = self_test(profile, canary, visible)

    assert denied is True
    assert allowed is True
    import subprocess

    binary = sandbox_exec_path()
    assert binary is not None
    env_read = subprocess.run(
        [binary, "-f", str(profile), "/bin/cat", str(project / ".env")],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert env_read.returncode != 0
    assert "sk-not-for-the-runtime" not in (env_read.stdout or "")
