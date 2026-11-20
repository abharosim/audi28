"""Unified approval center. DeepSeek uses it now; Claude Code and Codex can later.

There is no timeout. A pending request stays pending until a person decides.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from audi28.events import EventLog

_PATHISH = re.compile(r"(https?://\S+|~?/[\w./~-]+|[A-Za-z]:\\[\w.\\ -]+|sk-[A-Za-z0-9_-]+)")
_DECISIONS = {"allow", "deny", "cancel"}
_SCHEMA = (
    "id",
    "session_id",
    "source_agent",
    "source_label",
    "kind",
    "tool_name",
    "call_id",
    "reason",
    "summary",
    "detail",
    "questions",
    "created_ts",
    "idempotency_key",
)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_GUARD = threading.Lock()


@dataclass
class ApprovalRequest:
    id: str
    session_id: str
    source_agent: str
    source_label: str
    kind: str
    tool_name: str | None
    call_id: str | None
    reason: str | None
    summary: str
    detail: dict[str, Any]
    questions: list[dict[str, Any]] | None
    created_ts: str
    idempotency_key: str
    status: str = "pending"
    text: str = ""
    spoken_text: str = ""
    last_announced_ts: str = ""
    decision: str | None = None
    answers: list[dict[str, Any]] | None = None
    decision_id: str | None = None
    decision_message: str | None = None
    decision_source: str | None = None
    decision_ts: str | None = None
    delivered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in _SCHEMA}

    def command(self) -> str | None:
        value = self.detail.get("command") if isinstance(self.detail, dict) else None
        return value if isinstance(value, str) and value else None


@dataclass
class ApprovalDecision:
    id: str
    request_id: str
    decision: str
    message: str | None
    answers: list[dict[str, Any]] | None
    source: str
    ts: str
    delivered: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "decision": self.decision,
            "message": self.message,
            "answers": self.answers,
            "source": self.source,
            "ts": self.ts,
        }


@dataclass
class DecideResult:
    applied: bool
    request: ApprovalRequest | None
    decision: ApprovalDecision | None
    error: str | None


class ApprovalCenter:
    def __init__(
        self,
        directory: Path,
        *,
        events: EventLog | None = None,
        outbox: Any = None,
        clock=None,
    ) -> None:
        self.directory = Path(directory)
        self.events = events
        self.outbox = outbox
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._reannounced = False

    def bind(self, *, events: EventLog | None = None, outbox: Any = None) -> None:
        if events is not None:
            self.events = events
        if outbox is not None:
            self.outbox = outbox

    def create(self, **fields: Any) -> ApprovalRequest:
        kind = str(fields["kind"])
        session_id = str(fields.get("session_id") or "")
        backend_session_id = str(fields.get("backend_session_id") or "")
        rpc_id = str(fields.get("rpc_id") or "")
        call_id = fields.get("call_id")
        call = call_id if isinstance(call_id, str) and call_id else ""
        identity = call or rpc_id or uuid.uuid4().hex
        tool_name = fields.get("tool_name")
        reason = fields.get("reason")
        command = fields.get("command")
        questions = fields.get("questions")
        text = str(fields.get("text") or "")
        spoken = str(fields.get("spoken") or "")
        summary = _summary(str(tool_name or ""), str(reason or ""), kind)
        detail = {
            "raw_method": "user-question/ask" if kind == "user_question" else "approval/request",
            "backend_session_id": backend_session_id,
            "rpc_id": rpc_id,
        }
        if isinstance(command, str) and command:
            detail["command"] = command
        return self.open_request(
            {
                "session_id": session_id,
                "source_agent": "deepseek",
                "source_label": "DeepSeek",
                "kind": kind,
                "tool_name": tool_name if isinstance(tool_name, str) and tool_name else None,
                "call_id": call or None,
                "reason": reason if isinstance(reason, str) and reason else None,
                "summary": summary,
                "detail": detail,
                "questions": questions if isinstance(questions, list) else None,
                "idempotency_key": f"deepseek:{backend_session_id}:{identity}",
                "text": text,
                "spoken_text": spoken,
            }
        )

    def open_request(self, fields: dict[str, Any]) -> ApprovalRequest:
        with _guard(self.directory):
            key = str(fields.get("idempotency_key") or "")
            if key:
                existing = self._find_key(key)
                if existing is not None:
                    return existing
            now = self._timestamp()
            source_label = str(fields.get("source_label") or "DeepSeek")
            summary = _voice(str(fields.get("summary") or "")) or "有一条待处理的请求。"
            spoken = str(fields.get("spoken_text") or "")
            if not spoken.startswith(f"{source_label} 在问你："):
                spoken = f"{source_label} 在问你：{summary}"
            record = ApprovalRequest(
                id=f"apr-{uuid.uuid4().hex[:12]}",
                session_id=str(fields.get("session_id") or ""),
                source_agent=str(fields.get("source_agent") or "deepseek"),
                source_label=source_label,
                kind=str(fields.get("kind") or "approval"),
                tool_name=fields.get("tool_name"),
                call_id=fields.get("call_id"),
                reason=fields.get("reason"),
                summary=summary,
                detail=dict(fields.get("detail") or {}),
                questions=fields.get("questions"),
                created_ts=now,
                idempotency_key=key,
                status="pending",
                text=str(fields.get("text") or spoken),
                spoken_text=spoken,
                last_announced_ts=now,
            )
            self._write(record)
            self._audit(
                "approval",
                record.id,
                {
                    "action": "requested",
                    "id": record.id,
                    "kind": record.kind,
                    "source_agent": record.source_agent,
                    "source_label": record.source_label,
                    "tool_name": record.tool_name,
                    "summary": record.summary,
                    "status": "pending",
                    "idempotency_key": record.idempotency_key,
                },
            )
            self._announce(record, reminder=False)
            return record

    def get(self, request_id: str) -> ApprovalRequest | None:
        path = self._path(request_id)
        if path is None or not path.is_file():
            return None
        try:
            return _from_obj(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            return None

    def list_pending(self) -> list[ApprovalRequest]:
        if not self.directory.exists():
            return []
        records: list[ApprovalRequest] = []
        for path in sorted(self.directory.glob("apr-*.json")):
            try:
                record = _from_obj(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                continue
            if record.status == "pending":
                records.append(record)
        return records

    def list_all(self) -> list[ApprovalRequest]:
        if not self.directory.exists():
            return []
        records: list[ApprovalRequest] = []
        for path in sorted(self.directory.glob("apr-*.json")):
            try:
                records.append(_from_obj(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
                continue
        return records

    def decide(
        self,
        request_id: str,
        decision: str | None,
        answers: list[dict[str, Any]] | None = None,
        *,
        message: str | None = None,
        source: str = "user",
    ) -> DecideResult:
        with _guard(self.directory):
            record = self.get(request_id)
            if record is None:
                reason = "没有这条待处理的审批。"
                self._fail(request_id, reason, source_label="审批中心")
                return DecideResult(False, None, None, reason)
            if record.status != "pending":
                reason = "这条审批已经有决定了，没有再改。"
                self._fail(request_id, reason, source_label=record.source_label, source_agent=record.source_agent)
                return DecideResult(False, record, None, reason)
            if decision not in _DECISIONS:
                reason = "决定只能是允许、拒绝或取消。"
                self._fail(request_id, reason, source_label=record.source_label, source_agent=record.source_agent)
                return DecideResult(False, record, None, reason)
            made = ApprovalDecision(
                id=f"apd-{uuid.uuid4().hex[:12]}",
                request_id=record.id,
                decision=decision,
                message=message,
                answers=answers,
                source=source if source in {"user", "rule"} else "user",
                ts=self._timestamp(),
                delivered=False,
            )
            record.status = "decided"
            record.decision = made.decision
            record.answers = answers
            record.decision_id = made.id
            record.decision_message = message
            record.decision_source = made.source
            record.decision_ts = made.ts
            record.delivered = False
            self._write(record)
            self._audit(
                "decision",
                record.id,
                {
                    "action": "decided",
                    "id": made.id,
                    "request_id": record.id,
                    "decision": made.decision,
                    "source": made.source,
                    "status": "decided",
                    "message": message,
                },
            )
            return DecideResult(True, record, made, None)

    def resolve(
        self,
        request_id: str,
        decision: str | None,
        answers: list[dict[str, Any]] | None,
    ) -> ApprovalRequest | None:
        result = self.decide(request_id, decision, answers)
        return result.request if result.applied else None

    def reannounce_pending(self) -> int:
        if self._reannounced:
            return 0
        self._reannounced = True
        with _guard(self.directory):
            pending = self.list_pending()
            for record in pending:
                self._announce(record, reminder=True)
                record.last_announced_ts = self._timestamp()
                self._write(record)
            return len(pending)

    def remind_due(self, remind_after_s: float | None) -> int:
        if remind_after_s is None:
            return 0
        now = self._clock()
        sent = 0
        with _guard(self.directory):
            for record in self.list_pending():
                stamp = record.last_announced_ts or record.created_ts
                try:
                    then = datetime.fromisoformat(stamp)
                except ValueError:
                    continue
                if (now - then).total_seconds() < remind_after_s:
                    continue
                self._announce(record, reminder=True)
                record.last_announced_ts = self._timestamp()
                self._write(record)
                sent += 1
        return sent

    def undelivered(self) -> list[ApprovalDecision]:
        found: list[ApprovalDecision] = []
        for record in self.list_all():
            if record.status != "decided" or record.delivered or record.decision is None:
                continue
            found.append(
                ApprovalDecision(
                    id=record.decision_id or "",
                    request_id=record.id,
                    decision=record.decision,
                    message=record.decision_message,
                    answers=record.answers,
                    source=record.decision_source or "user",
                    ts=record.decision_ts or record.created_ts,
                    delivered=False,
                )
            )
        return found

    def mark_delivered(self, decision_id: str) -> None:
        if not decision_id:
            return
        with _guard(self.directory):
            for record in self.list_all():
                if record.decision_id != decision_id:
                    continue
                record.delivered = True
                self._write(record)
                return

    def _announce(self, record: ApprovalRequest, *, reminder: bool) -> None:
        if self.outbox is None:
            return
        payload: dict[str, Any] = {
            "kind": record.kind,
            "task_id": record.id,
            "session_id": record.session_id or None,
            "request_id": record.id,
            "source_agent": record.source_agent,
            "source_label": record.source_label,
            "status": "needs_decision",
            "text": record.text or record.spoken_text,
            "spoken": record.spoken_text,
            "spoken_text": record.spoken_text,
        }
        backend_session_id = record.detail.get("backend_session_id") if isinstance(record.detail, dict) else None
        if isinstance(backend_session_id, str) and backend_session_id:
            payload["backend_session_id"] = backend_session_id
        if reminder:
            payload["reminder"] = True
        self.outbox.enqueue(payload)

    def _fail(self, request_id: str, reason: str, *, source_label: str, source_agent: str = "audi28") -> None:
        self._audit("error", request_id, {"reason": reason, "request_id": request_id})
        if self.outbox is None:
            return
        self.outbox.enqueue(
            {
                "kind": "error",
                "request_id": request_id,
                "source_agent": source_agent,
                "source_label": source_label,
                "status": "failed",
                "text": reason,
                "spoken": reason,
                "spoken_text": reason,
            }
        )

    def _audit(self, event_type: str, ref: str | None, data: dict[str, Any]) -> None:
        if self.events is None:
            return
        safe = dict(data)
        if isinstance(safe.get("summary"), str):
            safe["summary"] = _voice(safe["summary"])
        self.events.append(type=event_type, source="approval", ref=ref, data=safe)

    def _find_key(self, key: str) -> ApprovalRequest | None:
        for record in self.list_all():
            if record.idempotency_key == key:
                return record
        return None

    def _path(self, request_id: str) -> Path | None:
        name = Path(request_id).name
        if not name or name != request_id or "/" in request_id or "\\" in request_id or name.startswith("."):
            return None
        return self.directory / f"{name}.json"

    def _write(self, record: ApprovalRequest) -> None:
        path = self._path(record.id)
        if path is None:
            raise ValueError("审批 id 不合法。")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.tmp"
        data = (json.dumps(_stored(record), ensure_ascii=False) + "\n").encode("utf-8")
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

    def _timestamp(self) -> str:
        moment = self._clock()
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("审批时间必须带时区。")
        return moment.isoformat(timespec="milliseconds")


def normalize_claude_permission_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a Claude Code PermissionRequest body onto ApprovalRequest fields.

    The body uses the fields named in docs/sources: tool_name, tool_input,
    permission_mode, and permission_suggestions. This does not implement the hook.
    """

    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool_name = _text(payload.get("tool_name"))
    command = _text(tool_input.get("command") or tool_input.get("file_path") or tool_input.get("path"))
    return {
        "source_agent": "claude",
        "source_label": "Claude Code",
        "kind": "approval",
        "tool_name": tool_name or None,
        "call_id": None,
        "reason": None,
        "summary": _summary(tool_name, "", "approval"),
        "detail": {
            "raw_method": "PermissionRequest",
            "permission_mode": payload.get("permission_mode"),
            "permission_suggestions": payload.get("permission_suggestions"),
            "tool_input": tool_input,
            "command": command or None,
        },
        "questions": None,
        "idempotency_key": "claude:" + _fingerprint(tool_name, tool_input),
    }


def normalize_codex_permission_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a Codex PermissionRequest body onto ApprovalRequest fields.

    The body uses the fields named in docs/sources: tool_name, tool_input,
    turn_id, and the optional tool_input.description. This does not implement the hook.
    """

    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool_name = _text(payload.get("tool_name"))
    turn_id = _text(payload.get("turn_id"))
    reason = _text(tool_input.get("description"))
    command = _text(tool_input.get("command") or tool_input.get("file_path") or tool_input.get("path"))
    return {
        "source_agent": "codex",
        "source_label": "Codex",
        "kind": "approval",
        "tool_name": tool_name or None,
        "call_id": turn_id or None,
        "reason": reason or None,
        "summary": _summary(tool_name, reason, "approval"),
        "detail": {
            "raw_method": "PermissionRequest",
            "turn_id": turn_id or None,
            "tool_input": tool_input,
            "command": command or None,
        },
        "questions": None,
        "idempotency_key": "codex:" + _fingerprint(turn_id or tool_name, tool_input),
    }


def tokens_match(expected: str, presented: str) -> bool:
    if not expected or not presented:
        return False
    return hmac.compare_digest(expected, presented)


def _stored(record: ApprovalRequest) -> dict[str, Any]:
    data = record.as_dict()
    data.update(
        status=record.status,
        text=record.text,
        spoken_text=record.spoken_text,
        last_announced_ts=record.last_announced_ts,
        decision=None
        if record.decision_id is None
        else {
            "id": record.decision_id,
            "request_id": record.id,
            "decision": record.decision,
            "message": record.decision_message,
            "answers": record.answers,
            "source": record.decision_source,
            "ts": record.decision_ts,
            "delivered": record.delivered,
        },
    )
    return data


def _from_obj(obj: dict[str, Any]) -> ApprovalRequest:
    made = obj.get("decision") if isinstance(obj.get("decision"), dict) else None
    return ApprovalRequest(
        id=str(obj["id"]),
        session_id=str(obj.get("session_id") or ""),
        source_agent=str(obj.get("source_agent") or "deepseek"),
        source_label=str(obj.get("source_label") or "DeepSeek"),
        kind=str(obj.get("kind") or "approval"),
        tool_name=obj.get("tool_name"),
        call_id=obj.get("call_id"),
        reason=obj.get("reason"),
        summary=str(obj.get("summary") or ""),
        detail=dict(obj.get("detail") or {}),
        questions=obj.get("questions"),
        created_ts=str(obj.get("created_ts") or ""),
        idempotency_key=str(obj.get("idempotency_key") or ""),
        status=str(obj.get("status") or "pending"),
        text=str(obj.get("text") or ""),
        spoken_text=str(obj.get("spoken_text") or ""),
        last_announced_ts=str(obj.get("last_announced_ts") or ""),
        decision=None if made is None else made.get("decision"),
        answers=None if made is None else made.get("answers"),
        decision_id=None if made is None else made.get("id"),
        decision_message=None if made is None else made.get("message"),
        decision_source=None if made is None else made.get("source"),
        decision_ts=None if made is None else made.get("ts"),
        delivered=bool(made.get("delivered")) if made is not None else False,
    )


def _summary(tool: str, reason: str, kind: str) -> str:
    if kind == "user_question":
        body = _voice(reason) or "有一个问题要你回答。"
        return body
    name = tool if tool and not _PATHISH.search(tool) else "一个工具"
    why = _voice(reason)
    if why:
        return f"申请使用工具 {name}。原因：{why}。只批准这一次。"
    return f"申请使用工具 {name}。只批准这一次。"


def _voice(text: str) -> str:
    cleaned = _PATHISH.sub("", text)
    return " ".join(cleaned.split())


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _fingerprint(head: str, payload: dict[str, Any]) -> str:
    raw = json.dumps({"head": head, "payload": payload}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _guard(directory: Path):
    return _ApprovalGuard(directory)


class _ApprovalGuard:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._thread: threading.Lock | None = None
        self._fd: int | None = None

    def __enter__(self) -> "_ApprovalGuard":
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
