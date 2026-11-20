#!/usr/bin/env python3
"""Run audi28 with an in-process fake DeepSeek.

No network call and no real DEEPSEEK_API_KEY. Inbox tasks still go through
the router, the session store, and the outbox. Approvals are not asked,
because this fake accepts the prompt and returns a short result.

    uv run python scripts/mock_deepseek.py
    uv run python scripts/mock_deepseek.py --config config.toml
"""

from __future__ import annotations

import os
import queue
import sys
from pathlib import Path

from audi28.config import ConfigError, prepare
from audi28.supervisor import supervise


class _Result:
    def __init__(self, text: str) -> None:
        self.final_response = text


class MockSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id

    def run(self, text: str) -> _Result:
        if text.startswith("/permission"):
            return _Result("")
        return _Result("这是假的 DeepSeek，没有调用接口。")


class MockHarness:
    def __init__(self, **_options: object) -> None:
        self.client = self
        self._queue: queue.Queue = queue.Queue()

    def start_session(self, session_id: str) -> MockSession:
        return MockSession(session_id)

    def resume_session(self, session_id: str) -> MockSession:
        return MockSession(session_id)

    def next_request(self):
        return self._queue.get()

    def respond(self, *_args: object, **_kwargs: object) -> None:
        return None

    def respond_error(self, *_args: object, **_kwargs: object) -> None:
        return None

    def close(self) -> None:
        self._queue.put(None)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config_path = Path.cwd() / "config.toml"
    if "--config" in args:
        index = args.index("--config")
        if index + 1 >= len(args):
            print("mock_deepseek 需要 --config 后面的路径。", file=sys.stderr)
            return 1
        config_path = Path(args[index + 1])
    if not os.environ.get("DEEPSEEK_API_KEY", "").strip():
        os.environ["DEEPSEEK_API_KEY"] = "mock"
    try:
        config = prepare(config_path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("audi28 正在用进程内的假 DeepSeek。不会访问网络。")
    supervise(config, harness_factory=lambda **options: MockHarness(**options))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
