"""Minimal pending approval and question store. No timeout, no dashboard."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class PendingRecord:
    id: str
    kind: str
    session_id: str
    backend_session_id: str
    rpc_id: str
    tool_name: str | None
    call_id: str | None
    reason: str | None
    command: str | None
    questions: list[dict[str, Any]] | None
    status: str
    decision: str | None
    answers: list[dict[str, Any]] | None
    created_ts: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "session_id": self.session_id,
            "backend_session_id": self.backend_session_id,
            "rpc_id": self.rpc_id,
            "tool_name": self.tool_name,
            "call_id": self.call_id,
            "reason": self.reason,
            "command": self.command,
            "questions": self.questions,
            "status": self.status,
            "decision": self.decision,
            "answers": self.answers,
            "created_ts": self.created_ts,
        }


class PendingStore:
    def __init__(self, directory: Path, *, clock=None) -> None:
        self.directory = Path(directory)
        self._clock = clock or (lambda: datetime.now().astimezone())

    def create(self, **fields: Any) -> PendingRecord:
        self.directory.mkdir(parents=True, exist_ok=True)
        record = PendingRecord(
            id=str(fields.get("id") or f"apr-{uuid.uuid4().hex[:12]}"),
            kind=str(fields["kind"]),
            session_id=str(fields.get("session_id") or ""),
            backend_session_id=str(fields.get("backend_session_id") or ""),
            rpc_id=str(fields.get("rpc_id") or ""),
            tool_name=fields.get("tool_name"),
            call_id=fields.get("call_id"),
            reason=fields.get("reason"),
            command=fields.get("command"),
            questions=fields.get("questions"),
            status="pending",
            decision=None,
            answers=None,
            created_ts=self._clock().isoformat(timespec="milliseconds"),
        )
        self._write(record)
        return record

    def get(self, request_id: str) -> PendingRecord | None:
        path = self._path(request_id)
        if not path.is_file():
            return None
        return _from_obj(json.loads(path.read_text(encoding="utf-8")))

    def list_pending(self) -> list[PendingRecord]:
        if not self.directory.exists():
            return []
        records: list[PendingRecord] = []
        for path in sorted(self.directory.glob("apr-*.json")):
            try:
                record = _from_obj(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                continue
            if record.status == "pending":
                records.append(record)
        return records

    def resolve(
        self,
        request_id: str,
        decision: str | None,
        answers: list[dict[str, Any]] | None,
    ) -> PendingRecord | None:
        record = self.get(request_id)
        if record is None:
            return None
        record.status = "decided"
        record.decision = decision
        record.answers = answers
        self._write(record)
        return record

    def _path(self, request_id: str) -> Path:
        name = Path(request_id).name
        return self.directory / f"{name}.json"

    def _write(self, record: PendingRecord) -> None:
        path = self._path(record.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.tmp"
        data = (json.dumps(record.as_dict(), ensure_ascii=False) + "\n").encode("utf-8")
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


def _from_obj(obj: dict[str, Any]) -> PendingRecord:
    return PendingRecord(
        id=str(obj["id"]),
        kind=str(obj["kind"]),
        session_id=str(obj.get("session_id") or ""),
        backend_session_id=str(obj.get("backend_session_id") or ""),
        rpc_id=str(obj.get("rpc_id") or ""),
        tool_name=obj.get("tool_name"),
        call_id=obj.get("call_id"),
        reason=obj.get("reason"),
        command=obj.get("command"),
        questions=obj.get("questions"),
        status=str(obj.get("status") or "pending"),
        decision=obj.get("decision"),
        answers=obj.get("answers"),
        created_ts=str(obj.get("created_ts") or ""),
    )
