"""Placeholder dashboard. It only binds 127.0.0.1."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class WebServer:
    def __init__(self, host: str, port: int) -> None:
        if host != "127.0.0.1":
            raise ValueError("web.host 只能是 127.0.0.1。")
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self.port = int(self._httpd.server_address[1])

    def serve_forever(self) -> None:
        self._httpd.serve_forever(poll_interval=0.1)

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        path = self.path.split("?", 1)[0]
        if path == "/health":
            body = json.dumps({"ok": True, "service": "audi28"}).encode("utf-8")
            self._send(200, "application/json", body)
            return
        self._send(200, "text/plain; charset=utf-8", b"audi28\n")

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
