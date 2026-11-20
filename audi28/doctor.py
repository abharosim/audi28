"""Report whether audi28 can start. Never prints secret values."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from audi28.config import ConfigError, load_config, load_dotenv


@dataclass(frozen=True)
class DoctorReport:
    text: str
    exit_code: int


def doctor_report(
    config_path: Path,
    environ: Mapping[str, str] | None = None,
) -> DoctorReport:
    """Check config and DEEPSEEK_API_KEY. A missing key is a message, not a crash."""

    env: dict[str, str] = dict(os.environ if environ is None else environ)
    dotenv_path = config_path.parent / ".env"
    load_dotenv(dotenv_path, env)

    lines: list[str] = []
    exit_code = 0
    if not config_path.is_file():
        lines.append(f"找不到配置文件：{config_path}。复制 config.example.toml 为 config.toml。")
        exit_code = 1
    else:
        try:
            load_config(config_path)
        except ConfigError as exc:
            lines.append(str(exc))
            exit_code = 1
        else:
            lines.append(f"{config_path.name} 可以加载。")

    if not env.get("DEEPSEEK_API_KEY", "").strip():
        exit_code = 1
        lines.append(
            "没有找到 DEEPSEEK_API_KEY。\n"
            f"把密钥写进 {dotenv_path}（这个文件已在 .gitignore 里；"
            "Mac 上的项目根目录是 ~/Code/audi28），或在环境变量里导出 DEEPSEEK_API_KEY。\n"
            "没有这把密钥时 audi28 不会崩溃，也不会去连接 DeepSeek。"
        )
    else:
        lines.append("DEEPSEEK_API_KEY 已设置（不显示值）。")

    return DoctorReport(text="\n".join(lines) + "\n", exit_code=exit_code)
