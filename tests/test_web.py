import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from audi28.config import load_config
from audi28.supervisor import supervise
from audi28.web import WebServer
from tests.conftest import write_config


def _wait_health(port: int) -> dict:
    deadline = time.time() + 3
    last_error: Exception | None = None
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                assert response.status == 200
                return json.loads(response.read().decode())
        except (urllib.error.URLError, AssertionError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            time.sleep(0.02)
    raise AssertionError(f"health check failed: {last_error}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_health_on_loopback():
    server = WebServer("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        body = _wait_health(server.port)
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert body == {"ok": True, "service": "audi28"}
    assert thread.is_alive() is False


def test_shutdown_returns_while_a_client_is_still_connected():
    server = WebServer("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _wait_health(server.port)
        held = socket.create_connection(("127.0.0.1", server.port), timeout=1)
        held.sendall(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        started = time.time()
        server.shutdown()
        assert time.time() - started < 3
    finally:
        thread.join(timeout=2)
    assert thread.is_alive() is False
    rebound = socket.socket()
    try:
        rebound.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rebound.bind(("127.0.0.1", server.port))
    finally:
        rebound.close()
        held.close()


def test_rejects_non_loopback():
    with pytest.raises(ValueError, match="127.0.0.1"):
        WebServer("0.0.0.0", 0)


def test_supervise_starts_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    port = _free_port()
    path = write_config(tmp_path, port=port)
    config = load_config(path)
    stop = threading.Event()
    thread = threading.Thread(target=lambda: supervise(config, stop=stop))
    thread.start()
    try:
        assert _wait_health(port)["ok"] is True
    finally:
        stop.set()
        thread.join(timeout=3)

    assert thread.is_alive() is False
