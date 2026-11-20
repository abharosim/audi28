"""macOS sandbox for the DeepSeek runtime.

deepseek-harness-sdk 0.2.2rc2 starts `dsh` from `HarnessConfig.dsh_bin`.
When that field is set, the process argv is `(dsh_bin, "--profile", profile)`.
There is no prefix or wrapper argument, so audi28 points `dsh_bin` at a small
script. The script execs `/usr/bin/sandbox-exec -f <profile> <real-dsh> "$@"`.
The API key is passed in the child environment by the SDK (`DEEPSEEK_API_KEY`),
not by letting the runtime read the project `.env`.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("audi28.sandbox")

_SYSTEM_READ = (
    "/usr",
    "/bin",
    "/sbin",
    "/System",
    "/Library",
    "/opt",
    "/opt/homebrew",
    "/dev",
    "/private/tmp",
    "/private/var/folders",
    "/var/folders",
    "/tmp",
)


@dataclass(frozen=True)
class SandboxSetup:
    enabled: bool
    active: bool
    profile_path: Path | None
    dsh_bin: str | None
    warning: str | None


def render_profile(
    *,
    home: Path,
    workspace: Path,
    dsh_home: Path,
    project_root: Path,
    extra_read_paths: tuple[Path, ...] = (),
    venv: Path | None = None,
    runtime_dir: Path | None = None,
    sandbox_dir: Path | None = None,
    temp_paths: tuple[Path, ...] = (),
) -> str:
    """SBPL profile. Last matching rule wins, so allows come after the home deny."""

    home_path = home.expanduser().resolve()
    # Last matching rule wins. Broad temp allows come first so a home that
    # happens to live under /var/folders is still denied afterwards.
    lines = [
        "(version 1)",
        "(allow default)",
    ]
    for path in _SYSTEM_READ:
        lines.append(f"(allow file-read* (subpath {_quote(Path(path))}))")
    for path in (*temp_paths, *_default_temps()):
        lines.append(f"(allow file-read* file-write* (subpath {_quote(path)}))")
    lines.append(f"(deny file-read* (subpath {_quote(home_path)}))")
    lines.append(f"(deny file-write* (subpath {_quote(home_path)}))")
    lines.append(f"(allow file-read* file-write* (subpath {_quote(workspace)}))")
    lines.append(f"(allow file-read* file-write* (subpath {_quote(dsh_home)}))")
    read_only = [project_root / ".venv" if venv is None else venv]
    if runtime_dir is not None:
        read_only.append(runtime_dir)
    if sandbox_dir is not None:
        read_only.append(sandbox_dir)
    read_only.extend(extra_read_paths)
    seen: set[str] = set()
    for path in read_only:
        key = str(path.expanduser().resolve())
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"(allow file-read* (subpath {_quote(path)}))")
    env_file = project_root / ".env"
    lines.append(f"(deny file-read* file-write* (literal {_quote(env_file)}))")
    return "\n".join(lines) + "\n"


def prepare_sandbox(
    *,
    enabled: bool,
    home: Path,
    workspace: Path,
    dsh_home: Path,
    project_root: Path,
    extra_read_paths: tuple[Path, ...] = (),
) -> SandboxSetup:
    if not enabled:
        return SandboxSetup(False, False, None, None, None)
    if sys.platform != "darwin" or not _sandbox_exec():
        warning = "没有 sandbox-exec，DeepSeek 不套沙箱继续跑。"
        log.warning(warning)
        return SandboxSetup(True, False, None, None, warning)
    launch = bundled_launch_args()
    if not launch:
        warning = "找不到捆绑的 dsh，DeepSeek 不套沙箱继续跑。"
        log.warning(warning)
        return SandboxSetup(True, False, None, None, warning)
    sandbox_dir = project_root / "data" / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    runtime_dir = Path(launch[0]).resolve().parent
    profile_path = sandbox_dir / "deepseek.sb"
    profile_path.write_text(
        render_profile(
            home=home,
            workspace=workspace,
            dsh_home=dsh_home,
            project_root=project_root,
            extra_read_paths=extra_read_paths,
            runtime_dir=runtime_dir,
            sandbox_dir=sandbox_dir,
        ),
        encoding="utf-8",
    )
    wrapper = sandbox_dir / "dsh-sandboxed"
    command = " ".join(shlex.quote(part) for part in launch)
    wrapper.write_text(
        "#!/bin/sh\n"
        f"exec /usr/bin/sandbox-exec -f {shlex.quote(str(profile_path))} {command} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return SandboxSetup(True, True, profile_path, str(wrapper), None)


def bundled_launch_args() -> tuple[str, ...] | None:
    try:
        from deepseek_harness_runtime import resolve_bundled_launch_args
    except ImportError:
        return None
    try:
        launch = tuple(resolve_bundled_launch_args())
    except Exception:
        log.warning("读取捆绑 dsh 路径失败，DeepSeek 不套沙箱继续跑。", exc_info=True)
        return None
    if not launch or not launch[0]:
        return None
    return launch


def sandbox_exec_path() -> str | None:
    return _sandbox_exec()


def self_test(profile: Path, denied_file: Path, allowed_file: Path) -> tuple[bool, bool]:
    """Return (home file denied, workspace file readable)."""

    binary = _sandbox_exec()
    if binary is None:
        return False, False
    denied = _cat(binary, profile, denied_file).returncode != 0
    allowed_run = _cat(binary, profile, allowed_file)
    expected = allowed_file.read_text(encoding="utf-8") if allowed_file.is_file() else ""
    allowed = allowed_run.returncode == 0 and expected.strip() in (allowed_run.stdout or "")
    return denied, allowed


def _cat(binary: str, profile: Path, target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, "-f", str(profile), "/bin/cat", str(target)],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def _sandbox_exec() -> str | None:
    found = shutil.which("sandbox-exec")
    if found:
        return found
    if Path("/usr/bin/sandbox-exec").is_file():
        return "/usr/bin/sandbox-exec"
    return None


def _default_temps() -> tuple[Path, ...]:
    paths = [Path("/tmp"), Path("/private/tmp"), Path("/var/folders"), Path("/private/var/folders")]
    raw = os.environ.get("TMPDIR", "").strip()
    if raw:
        paths.append(Path(raw))
    return tuple(paths)


def _quote(path: Path) -> str:
    text = str(path.expanduser())
    if path.is_absolute() or str(path).startswith("~"):
        try:
            text = str(path.expanduser().resolve())
        except OSError:
            text = str(path.expanduser())
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
