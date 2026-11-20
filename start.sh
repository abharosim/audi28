#!/usr/bin/env bash
# Start the audi28 supervisor. The dashboard listens on 127.0.0.1:8728
# unless --config points somewhere else. When [deepseek] enabled = true,
# install the optional deepseek extra so the real SDK is available.
set -euo pipefail
cd "$(dirname "$0")"

config_path="config.toml"
previous=""
for arg in "$@"; do
  if [[ "$previous" == "--config" ]]; then
    config_path="$arg"
  fi
  previous="$arg"
done

extra=()
if [[ -f "$config_path" ]] && uv run python -c '
import sys
from pathlib import Path
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
cfg = tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
deepseek = cfg.get("deepseek")
raise SystemExit(0 if isinstance(deepseek, dict) and deepseek.get("enabled") is True else 1)
' "$config_path"; then
  extra+=(--extra deepseek)
fi

# Bash 3.2 on macOS treats "${extra[@]}" as unbound when the array is empty.
if [[ ${#extra[@]} -gt 0 ]]; then
  exec uv run "${extra[@]}" audi28 supervise "$@"
fi
exec uv run audi28 supervise "$@"
