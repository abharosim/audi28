"""Persistent audi28 session records. The files are the source of truth."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_CONTINUABLE = {"open", "idle", "running"}


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
        with self._lock():
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
        record = self.get(session_id)
        if record is None:
            raise SessionNotFound(session_id)
        closed = Session(
            session_id=record.session_id,
            backend=record.backend,
            backend_session_id=record.backend_session_id,
            status="closed",
            title=record.title,
            created_ts=record.created_ts,
            updated_ts=self._timestamp(),
            pending_questions=record.pending_questions,
            workspace=record.workspace,
            last_task_id=record.last_task_id,
        )
        with self._lock():
            _atomic_json(self._path(session_id), closed.as_dict())
        return closed

    def touch(self, session_id: str, *, task_id: str | None) -> Session:
        record = self.get(session_id)
        if record is None:
            raise SessionNotFound(session_id)
        updated = Session(
            session_id=record.session_id,
            backend=record.backend,
            backend_session_id=record.backend_session_id,
            status=record.status,
            title=record.title,
            created_ts=record.created_ts,
            updated_ts=self._timestamp(),
            pending_questions=record.pending_questions,
            workspace=record.workspace,
            last_task_id=task_id if task_id is not None else record.last_task_id,
        )
        with self._lock():
            _atomic_json(self._path(session_id), updated.as_dict())
        return updated

    def set_handle(self, session_id: str, backend_session_id: str) -> Session:
        record = self.get(session_id)
        if record is None:
            raise SessionNotFound(session_id)
        updated = Session(
            session_id=record.session_id,
            backend=record.backend,
            backend_session_id=backend_session_id,
            status=record.status,
            title=record.title,
            created_ts=record.created_ts,
            updated_ts=record.updated_ts,
            pending_questions=record.pending_questions,
            workspace=record.workspace,
            last_task_id=record.last_task_id,
        )
        with self._lock():
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

    def _lock(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _LockedFd(fd)


class _LockedFd:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def __enter__(self) -> "_LockedFd":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)


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
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
