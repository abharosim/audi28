"""Process supervisor. Phase 1 skeleton only keeps the local dashboard up."""

from __future__ import annotations

import signal
import threading

from audi28.config import Config
from audi28.web import WebServer


def supervise(config: Config, *, stop: threading.Event | None = None) -> None:
    """Run until stop is set, or until SIGINT/SIGTERM when stop is omitted."""

    server = WebServer(config.web.host, config.web.port)
    thread = threading.Thread(target=server.serve_forever, name="audi28-web")
    thread.start()
    try:
        if stop is None:
            _wait_for_signal()
        else:
            stop.wait()
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _wait_for_signal() -> None:
    stop = threading.Event()

    def handle(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    stop.wait()
