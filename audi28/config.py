"""Load audi28 config.toml and the project .env file."""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(ValueError):
    """config.toml or .env could not be used."""


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int


@dataclass(frozen=True)
class EventsConfig:
    pass


@dataclass(frozen=True)
class InboxConfig:
    poll_s: float
    parse_retries: int
    max_bytes: int


@dataclass(frozen=True)
class GrokbotConfig:
    header_name: str
    header_value: str
    timeout_s: float


@dataclass(frozen=True)
class RouteConfig:
    llm: bool
    default: str
    new_task_phrases: tuple[str, ...]
    not_new_task_phrases: tuple[str, ...]
    self_phrases: tuple[str, ...]


@dataclass(frozen=True)
class SessionsConfig:
    idle_timeout_s: int


@dataclass(frozen=True)
class DeepSeekConfig:
    enabled: bool
    profile: str
    dsh_home: Path
    workspace: Path
    provider: str
    model: str
    permission_mode: str
    approval_requests: bool
    initialize_timeout_seconds: float
    reasoning_effort: str | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class ApprovalConfig:
    """Pending approvals wait for the user. There is no timeout.

    remind_after_s is optional. None means the question is not repeated.
    A reminder never turns into a deny.
    """

    remind_after_s: float | None = None


@dataclass(frozen=True)
class HooksConfig:
    enabled: bool
    token_env: str


@dataclass(frozen=True)
class Config:
    root: Path
    web: WebConfig
    events: EventsConfig
    inbox: InboxConfig
    grokbot: GrokbotConfig
    route: RouteConfig
    sessions: SessionsConfig
    deepseek: DeepSeekConfig
    approval: ApprovalConfig
    hooks: HooksConfig


# required keys, optional keys
_SPECS: dict[str, tuple[set[str], set[str]]] = {
    "web": ({"host", "port"}, set()),
    "events": (set(), set()),
    "inbox": ({"poll_s", "parse_retries", "max_bytes"}, set()),
    "grokbot": ({"header_name", "header_value", "timeout_s"}, set()),
    "route": (
        {"llm", "default", "new_task_phrases", "not_new_task_phrases", "self_phrases"},
        set(),
    ),
    "sessions": ({"idle_timeout_s"}, set()),
    "deepseek": (
        {
            "enabled",
            "profile",
            "dsh_home",
            "workspace",
            "provider",
            "model",
            "permission_mode",
            "approval_requests",
            "initialize_timeout_seconds",
        },
        {"reasoning_effort", "max_tokens"},
    ),
    "approval": (set(), {"remind_after_s"}),
    "hooks": ({"enabled", "token_env"}, set()),
}


def load_dotenv(path: Path, environ: MutableMapping[str, str]) -> None:
    """Fill environ from a .env file. Existing keys win. Missing file is fine."""

    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in environ:
            continue
        environ[key] = _unquote(value.strip())


def load_config(path: Path) -> Config:
    """Load and validate config.toml. Secrets stay in the environment, not here."""

    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}。复制 config.example.toml 为 config.toml。")
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: 无法解析 TOML：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: 配置必须是一张表。")

    known = set(_SPECS)
    for section in raw:
        if section not in known:
            names = ", ".join(sorted(known))
            raise ConfigError(f"未知 section「{section}」。已知 section：{names}。")
    for section in known:
        if section not in raw:
            raise ConfigError(f"缺少 section「{section}」。")
        if not isinstance(raw[section], dict):
            raise ConfigError(f"section「{section}」必须是一张表。")
        _reject_unknown_keys(section, raw[section])

    root = path.parent.resolve()
    return Config(
        root=root,
        web=_web(raw["web"]),
        events=EventsConfig(),
        inbox=_inbox(raw["inbox"]),
        grokbot=_grokbot(raw["grokbot"]),
        route=_route(raw["route"]),
        sessions=_sessions(raw["sessions"]),
        deepseek=_deepseek(raw["deepseek"], root),
        approval=_approval(raw["approval"]),
        hooks=_hooks(raw["hooks"]),
    )


def prepare(path: Path, environ: MutableMapping[str, str] | None = None) -> Config:
    """Load the sibling .env, then config.toml. Does not require DEEPSEEK_API_KEY."""

    env = os.environ if environ is None else environ
    load_dotenv(path.parent / ".env", env)
    return load_config(path)


def _reject_unknown_keys(section: str, table: Mapping[str, object]) -> None:
    required, optional = _SPECS[section]
    allowed = required | optional
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"未知配置项 {section}.{unknown[0]}。")
    missing = sorted(required - set(table))
    if missing:
        raise ConfigError(f"缺少配置项 {section}.{missing[0]}。")


def _web(table: Mapping[str, object]) -> WebConfig:
    host = _require_str("web", "host", table["host"])
    if host != "127.0.0.1":
        raise ConfigError("web.host 只能是 127.0.0.1。")
    port = _require_int("web", "port", table["port"])
    if not 1 <= port <= 65535:
        raise ConfigError("web.port 必须在 1 到 65535 之间。")
    return WebConfig(host=host, port=port)


def _inbox(table: Mapping[str, object]) -> InboxConfig:
    return InboxConfig(
        poll_s=_require_number("inbox", "poll_s", table["poll_s"]),
        parse_retries=_require_int("inbox", "parse_retries", table["parse_retries"], minimum=0),
        max_bytes=_require_int("inbox", "max_bytes", table["max_bytes"], minimum=1),
    )


def _grokbot(table: Mapping[str, object]) -> GrokbotConfig:
    return GrokbotConfig(
        header_name=_require_str("grokbot", "header_name", table["header_name"]),
        header_value=_require_str("grokbot", "header_value", table["header_value"]),
        timeout_s=_require_number("grokbot", "timeout_s", table["timeout_s"]),
    )


def _route(table: Mapping[str, object]) -> RouteConfig:
    default = _require_str("route", "default", table["default"])
    if default == "self":
        default = "audi_self"
    if default not in {"deepseek", "audi_self"}:
        raise ConfigError("route.default 只能是 deepseek 或 audi_self。")
    return RouteConfig(
        llm=_require_bool("route", "llm", table["llm"]),
        default=default,
        new_task_phrases=_phrase_list("route", "new_task_phrases", table["new_task_phrases"], allow_empty=False),
        not_new_task_phrases=_phrase_list(
            "route",
            "not_new_task_phrases",
            table["not_new_task_phrases"],
            allow_empty=True,
        ),
        self_phrases=_phrase_list("route", "self_phrases", table["self_phrases"], allow_empty=True),
    )


def _phrase_list(section: str, key: str, value: object, *, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ConfigError(f"{section}.{key} 必须是字符串列表。")
    cleaned: list[str] = []
    for phrase in value:
        if not isinstance(phrase, str) or not phrase.strip():
            raise ConfigError(f"{section}.{key} 必须是字符串列表。")
        cleaned.append(phrase.strip())
    return tuple(cleaned)


def _sessions(table: Mapping[str, object]) -> SessionsConfig:
    return SessionsConfig(
        idle_timeout_s=_require_int(
            "sessions",
            "idle_timeout_s",
            table["idle_timeout_s"],
            minimum=1,
        )
    )


def _deepseek(table: Mapping[str, object], root: Path) -> DeepSeekConfig:
    reasoning = table.get("reasoning_effort")
    if reasoning is not None:
        reasoning = _require_str("deepseek", "reasoning_effort", reasoning)
    max_tokens = table.get("max_tokens")
    if max_tokens is not None:
        max_tokens = _require_int("deepseek", "max_tokens", max_tokens, minimum=1)
    return DeepSeekConfig(
        enabled=_require_bool("deepseek", "enabled", table["enabled"]),
        profile=_require_str("deepseek", "profile", table["profile"]),
        dsh_home=_project_path(root, _require_str("deepseek", "dsh_home", table["dsh_home"])),
        workspace=_workspace(_require_str("deepseek", "workspace", table["workspace"])),
        provider=_require_str("deepseek", "provider", table["provider"]),
        model=_require_str("deepseek", "model", table["model"]),
        permission_mode=_permission_mode(table["permission_mode"]),
        approval_requests=_require_bool(
            "deepseek",
            "approval_requests",
            table["approval_requests"],
        ),
        initialize_timeout_seconds=_require_number(
            "deepseek",
            "initialize_timeout_seconds",
            table["initialize_timeout_seconds"],
        ),
        reasoning_effort=reasoning,
        max_tokens=max_tokens,
    )


def _approval(table: Mapping[str, object]) -> ApprovalConfig:
    if "remind_after_s" not in table:
        return ApprovalConfig(remind_after_s=None)
    return ApprovalConfig(
        remind_after_s=_require_number("approval", "remind_after_s", table["remind_after_s"])
    )


def _hooks(table: Mapping[str, object]) -> HooksConfig:
    return HooksConfig(
        enabled=_require_bool("hooks", "enabled", table["enabled"]),
        token_env=_require_str("hooks", "token_env", table["token_env"]),
    )


_PHASE1_PERMISSION_MODES = {"read-only", "workspace-write"}


def _permission_mode(value: object) -> str:
    mode = _require_str("deepseek", "permission_mode", value)
    if mode not in _PHASE1_PERMISSION_MODES:
        raise ConfigError(
            "deepseek.permission_mode 在 Phase 1 只能是 read-only 或 workspace-write，"
            "不接受 danger-full-access。"
        )
    return mode


def _workspace(value: str) -> Path:
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        raise ConfigError(
            "deepseek.workspace 必须是绝对路径，或使用 ~。相对路径不会当作 $HOME 下的目录。"
        )
    return expanded


def _project_path(root: Path, value: str) -> Path:
    expanded = Path(value).expanduser()
    if expanded.is_absolute():
        return expanded
    return (root / expanded).resolve()


def _require_str(section: str, key: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section}.{key} 必须是非空字符串。")
    return value.strip()


def _require_bool(section: str, key: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{section}.{key} 必须是布尔值。")
    return value


def _require_int(section: str, key: str, value: object, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{section}.{key} 必须是整数。")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{section}.{key} 不能小于 {minimum}。")
    return value


def _require_number(section: str, key: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{section}.{key} 必须是数字。")
    if value <= 0:
        raise ConfigError(f"{section}.{key} 必须大于 0。")
    return float(value)


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value
