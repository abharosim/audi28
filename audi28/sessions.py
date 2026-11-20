"""Persistent audi28 session records. The files are the source of truth."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_CONTINUABLE = {"open", "idle", "running"}
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_GUARD = threading.Lock()


class SessionNotFound(LookupError):
    """No session file for this id."""


@dataclass(frozen=True)
class Session:
    session_id: str
    backend: str
    backend_session_id: str
    status: str
    title: str
    created_ts: str
    updated_ts: str
    pending_questions: int
    workspace: str
    last_task_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "backend": self.backend,
            "backend_session_id": self.backend_session_id,
            "status": self.status,
            "title": self.title,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "pending_questions": self.pending_questions,
            "workspace": self.workspace,
            "last_task_id": self.last_task_id,
        }


class SessionStore:
    def __init__(self, directory: Path, *, clock: Callable[[], datetime] | None = None) -> None:
        self.directory = Path(directory)
        self._clock = clock or (lambda: datetime.now().astimezone())

    def create(
        self,
        *,
        title: str,
        backend: str,
        backend_session_id: str,
        workspace: str,
        task_id: str | None,
    ) -> Session:
        self.directory.mkdir(parents=True, exist_ok=True)
        now = self._timestamp()
        record = Session(
            session_id=f"ses-{uuid.uuid4().hex}",
            backend=backend,
            backend_session_id=backend_session_id,
            status="open",
            title=_title(title),
            created_ts=now,
            updated_ts=now,
            pending_questions=0,
            workspace=workspace,
            last_task_id=task_id,
        )
        with self._guard():
            _atomic_json(self._path(record.session_id), record.as_dict())
        return record

    def get(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        if not path.is_file():
            return None
        return _session_from_obj(json.loads(path.read_text(encoding="utf-8")))

    def list(self, *, include_closed: bool = False) -> list[Session]:
        if not self.directory.exists():
            return []
        records: list[Session] = []
        for path in sorted(self.directory.glob("ses-*.json")):
            try:
                record = _session_from_obj(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                continue
            if record.status == "closed" and not include_closed:
                continue
            records.append(record)
        records.sort(key=lambda record: record.updated_ts, reverse=True)
        return records

    def close(self, session_id: str) -> Session:
        with self._guard():
            record = self._read(session_id)
            if record is None:
                raise SessionNotFound(session_id)
            if record.status == "closed":
                return record
            closed = _replace(
                record,
                status="closed",
                updated_ts=self._timestamp(),
            )
            _atomic_json(self._path(session_id), closed.as_dict())
            return closed

    def touch(self, session_id: str, *, task_id: str | None) -> Session:
        with self._guard():
            record = self._read(session_id)
            if record is None:
                raise SessionNotFound(session_id)
            if record.status == "closed":
                return record
            updated = _replace(
                record,
                updated_ts=self._timestamp(),
                last_task_id=task_id if task_id is not None else record.last_task_id,
            )
            _atomic_json(self._path(session_id), updated.as_dict())
            return updated

    def set_handle(self, session_id: str, backend_session_id: str) -> Session:
        with self._guard():
            record = self._read(session_id)
            if record is None:
                raise SessionNotFound(session_id)
            updated = _replace(record, backend_session_id=backend_session_id)
            _atomic_json(self._path(session_id), updated.as_dict())
            return updated

    def most_recent_continuable(self) -> Session | None:
        continuable = [record for record in self.list(include_closed=True) if record.status in _CONTINUABLE]
        if not continuable:
            return None
        return max(continuable, key=lambda record: record.updated_ts)

    def _timestamp(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("session 时间必须带时区。")
        return moment.isoformat(timespec="milliseconds")

    def _path(self, session_id: str) -> Path:
        if (
            not session_id
            or session_id != Path(session_id).name
            or "/" in session_id
            or "\\" in session_id
            or session_id.startswith(".")
        ):
            raise SessionNotFound(session_id)
        return self.directory / f"{session_id}.json"

    def _read(self, session_id: str) -> Session | None:
        path = self._path(session_id)
        if not path.is_file():
            return None
        return _session_from_obj(json.loads(path.read_text(encoding="utf-8")))

    def _guard(self):
        return _SessionGuard(self.directory)


def _replace(record: Session, **changes: Any) -> Session:
    data = record.as_dict()
    data.update(changes)
    return _session_from_obj(data)


class _SessionGuard:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._thread: threading.Lock | None = None
        self._fd: int | None = None

    def __enter__(self) -> "_SessionGuard":
        self._directory.mkdir(parents=True, exist_ok=True)
        self._thread = _thread_lock(self._directory)
        self._thread.acquire()
        self._fd = os.open(self._directory / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        if self._thread is not None:
            self._thread.release()


def _thread_lock(directory: Path) -> threading.Lock:
    key = str(directory.resolve())
    with _THREAD_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _title(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= 40:
        return compact
    return compact[:40]


def _session_from_obj(obj: object) -> Session:
    if not isinstance(obj, dict):
        raise TypeError("session 文件必须是对象。")
    return Session(
        session_id=str(obj["session_id"]),
        backend=str(obj["backend"]),
        backend_session_id=str(obj.get("backend_session_id") or ""),
        status=str(obj["status"]),
        title=str(obj.get("title") or ""),
        created_ts=str(obj["created_ts"]),
        updated_ts=str(obj["updated_ts"]),
        pending_questions=int(obj.get("pending_questions") or 0),
        workspace=str(obj.get("workspace") or ""),
        last_task_id=obj.get("last_task_id") if isinstance(obj.get("last_task_id"), str) else None,
    )


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.tmp"
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        view = data
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(f"写入 {path.name} 失败")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
