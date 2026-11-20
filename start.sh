#!/usr/bin/env bash
# Start the audi28 supervisor. The dashboard listens on 127.0.0.1:8728
# unless --config points somewhere else.
set -euo pipefail
cd "$(dirname "$0")"
exec uv run audi28 supervise "$@"
