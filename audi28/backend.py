"""Backend hand-off. The DeepSeek adapter arrives in a later step."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class BackendNotConfigured(RuntimeError):
    """The backend cannot run because a required secret is missing."""


class Backend(Protocol):
    def start(self, session_id: str, text: str) -> str:
        """Open a backend session and return its handle."""

    def run(self, backend_session_id: str, text: str) -> None:
        """Send a follow-up to an existing backend session."""


@dataclass(frozen=True)
class StartCall:
    session_id: str
    text: str
    handle: str


@dataclass(frozen=True)
class RunCall:
    backend_session_id: str
    text: str


class FakeBackend:
    """Records calls. Tests use this instead of DeepSeek."""

    def __init__(self) -> None:
        self.starts: list[StartCall] = []
        self.runs: list[RunCall] = []

    def start(self, session_id: str, text: str) -> str:
        handle = f"fake-{len(self.starts) + 1}"
        self.starts.append(StartCall(session_id=session_id, text=text, handle=handle))
        return handle

    def run(self, backend_session_id: str, text: str) -> None:
        self.runs.append(RunCall(backend_session_id=backend_session_id, text=text))


class UnconnectedBackend:
    """Used until the DeepSeek adapter is installed. It does not open a network session."""

    def start(self, session_id: str, text: str) -> str:
        del session_id, text
        return ""

    def run(self, backend_session_id: str, text: str) -> None:
        del backend_session_id, text
        return None
