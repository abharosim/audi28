"""Append-only JSONL event log.

Writers take an exclusive lock and fsync. Readers skip a half-written last line.
Unknown event types are stored and returned as-is.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class EventLogError(ValueError):
    """The event log could not be written or a complete line was unreadable."""


@dataclass(frozen=True)
class Event:
    id: str
    ts: str
    type: str
    source: str
    ref: str | None
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "type": self.type,
            "source": self.source,
            "ref": self.ref,
            "data": self.data,
        }


class EventLog:
    def __init__(
        self,
        directory: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.directory = Path(directory)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._id_factory = id_factory or (lambda: f"evt-{uuid.uuid4().hex}")

    def append(
        self,
        *,
        type: str,
        source: str,
        ref: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Event:
        if not isinstance(type, str) or not type:
            raise EventLogError("type 必须是非空字符串。")
        if not isinstance(source, str) or not source:
            raise EventLogError("source 必须是非空字符串。")
        if ref is not None and not isinstance(ref, str):
            raise EventLogError("ref 必须是字符串或 null。")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise EventLogError("data 必须是对象。")

        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise EventLogError("事件时间必须带时区。")
        event = Event(
            id=self._id_factory(),
            ts=moment.isoformat(timespec="milliseconds"),
            type=type,
            source=source,
            ref=ref,
            data=data,
        )
        line = json.dumps(event.as_dict(), ensure_ascii=False) + "\n"
        self._append_line(moment.date(), line.encode("utf-8"))
        return event

    def replay(self, day: date | None = None) -> list[Event]:
        if not self.directory.exists():
            return []
        if day is not None:
            paths = [self.directory / f"{day.isoformat()}.jsonl"]
        else:
            paths = sorted(self.directory.glob("*.jsonl"))
        events: list[Event] = []
        for path in paths:
            if not path.is_file():
                continue
            events.extend(_events_in_file(path))
        return events

    def follow(
        self,
        *,
        poll_s: float = 0.2,
        ready: threading.Event | None = None,
    ) -> Iterator[Event]:
        """Yield events appended after this call starts."""

        offsets: dict[Path, int] = {}
        self._snapshot(offsets)
        if ready is not None:
            ready.set()
        while True:
            if self.directory.exists():
                for path in sorted(self.directory.glob("*.jsonl")):
                    yield from self._drain(path, offsets)
            time.sleep(poll_s)

    def _append_line(self, day: date, payload: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{day.isoformat()}.jsonl"
        thread_lock = _thread_lock_for(path)
        with thread_lock:
            created = not path.exists()
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    _drop_partial_tail(fd)
                    os.lseek(fd, 0, os.SEEK_END)
                    view = payload
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise EventLogError(f"写入事件失败：{path}")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            if created:
                _fsync_directory(self.directory)

    def _snapshot(self, offsets: dict[Path, int]) -> None:
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("*.jsonl")):
            offsets[path] = len(_complete_bytes(path.read_bytes()))

    def _drain(self, path: Path, offsets: dict[Path, int]) -> Iterator[Event]:
        complete = _complete_bytes(path.read_bytes())
        offset = offsets.get(path, 0)
        if offset > len(complete):
            offset = 0
        chunk = complete[offset:]
        offsets[path] = len(complete)
        if not chunk:
            return
        yield from _events_from_bytes(chunk, path)


def _thread_lock_for(path: Path) -> threading.Lock:
    # flock is per process, so threads in this process need their own lock.
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _drop_partial_tail(fd: int) -> None:
    size = os.lseek(fd, 0, os.SEEK_END)
    if size == 0:
        return
    os.lseek(fd, size - 1, os.SEEK_SET)
    if os.read(fd, 1) == b"\n":
        return
    os.lseek(fd, 0, os.SEEK_SET)
    content = os.read(fd, size)
    cut = content.rfind(b"\n")
    keep = 0 if cut < 0 else cut + 1
    os.ftruncate(fd, keep)


def _fsync_directory(directory: Path) -> None:
    dirfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def _complete_bytes(raw: bytes) -> bytes:
    if not raw or raw.endswith(b"\n"):
        return raw
    cut = raw.rfind(b"\n")
    if cut < 0:
        return b""
    return raw[: cut + 1]


def _events_in_file(path: Path) -> list[Event]:
    return _events_from_bytes(_complete_bytes(path.read_bytes()), path)


def _events_from_bytes(raw: bytes, path: Path) -> list[Event]:
    if not raw:
        return []
    events: list[Event] = []
    text = raw.decode("utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path}:{line_no}"
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EventLogError(f"{where}: 不是合法 JSON。") from exc
        events.append(_event_from_obj(obj, where))
    return events


def _event_from_obj(obj: object, where: str) -> Event:
    if not isinstance(obj, dict):
        raise EventLogError(f"{where}: 事件必须是 JSON 对象。")
    for field in ("id", "ts", "type", "source", "ref", "data"):
        if field not in obj:
            raise EventLogError(f"{where}: 缺少字段 {field}。")
    data = obj["data"]
    if not isinstance(data, dict):
        raise EventLogError(f"{where}: data 必须是对象。")
    ref = obj["ref"]
    if ref is not None and not isinstance(ref, str):
        raise EventLogError(f"{where}: ref 必须是字符串或 null。")
    return Event(
        id=str(obj["id"]),
        ts=str(obj["ts"]),
        type=str(obj["type"]),
        source=str(obj["source"]),
        ref=ref,
        data=data,
    )
