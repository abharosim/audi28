"""Turn one inbox message into a route event, a session event, then a backend call."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from audi28.backend import Backend, BackendNotConfigured
from audi28.events import EventLog
from audi28.router import RouteContext, Router, SessionSnapshot, TaskMessage
from audi28.sessions import Session, SessionNotFound, SessionStore

_REJECTED = {"closed", "error"}


class Dispatcher:
    def __init__(
        self,
        *,
        router: Router,
        sessions: SessionStore,
        backend: Backend,
        events: EventLog,
        workspace: str,
        outbox: Any = None,
    ) -> None:
        self.router = router
        self.sessions = sessions
        self.backend = backend
        self.events = events
        self.workspace = workspace
        self.outbox = outbox

    def handle(self, message: Mapping[str, Any]) -> None:
        message_id = _message_id(message)
        if message_id and self._is_done(message_id):
            return
        kind = message.get("kind")
        if kind == "close_session":
            self._close(message)
        elif kind == "task":
            self._dispatch_task(message)
        elif kind == "decision":
            self._decide(message)
        if message_id:
            self._mark_done(message_id)

    def _dispatch_task(self, message: Mapping[str, Any]) -> None:
        task = _task_message(message)
        if task.message_id and self._already_routed(task.message_id):
            return
        decision = self.router.decide(task, self._context())
        self.events.append(
            type="route",
            source="router",
            ref=task.message_id,
            data=decision.as_dict(),
        )
        if decision.target == "audi_self" or decision.session_action == "none":
            return
        if decision.session_action == "continue" and decision.session_id:
            self._continue(task, decision.session_id, decision.reason)
            return
        self._open(task, decision.reason)

    def _context(self) -> RouteContext:
        snapshots = tuple(
            SessionSnapshot(
                session_id=record.session_id,
                status=record.status,
                updated_ts=record.updated_ts,
            )
            for record in self.sessions.list(include_closed=True)
        )
        return RouteContext(sessions=snapshots)

    def _is_done(self, message_id: str) -> bool:
        return self._has_event("inbox_done", message_id)

    def _already_routed(self, message_id: str) -> bool:
        return self._has_event("route", message_id)

    def _has_event(self, event_type: str, message_id: str) -> bool:
        for event in self.events.replay():
            if event.type == event_type and event.ref == message_id:
                return True
        return False

    def _mark_done(self, message_id: str) -> None:
        if self._is_done(message_id):
            return
        self.events.append(
            type="inbox_done",
            source="dispatch",
            ref=message_id,
            data={"message_id": message_id},
        )

    def _open(self, task: TaskMessage, reason: str) -> None:
        record = self.sessions.create(
            title=task.text,
            backend="deepseek",
            backend_session_id="",
            workspace=self.workspace,
            task_id=task.message_id,
        )
        self._session_event("created", record, reason)
        try:
            handle = self.backend.start(record.session_id, task.text)
        except BackendNotConfigured as exc:
            self._not_configured(task.message_id, str(exc))
            return
        if handle:
            record = self.sessions.set_handle(record.session_id, handle)

    def _continue(self, task: TaskMessage, session_id: str, reason: str) -> None:
        record = self.sessions.get(session_id)
        if record is None or record.status in _REJECTED:
            self.events.append(
                type="error",
                source="sessions",
                ref=task.message_id,
                data={"reason": "session 已关闭或不存在", "session_id": session_id},
            )
            return
        record = self.sessions.touch(session_id, task_id=task.message_id)
        if record.status in _REJECTED:
            if task.session_id:
                self.events.append(
                    type="error",
                    source="sessions",
                    ref=task.message_id,
                    data={
                        "reason": "消息指定的 session 已关闭，没有另开一个。",
                        "session_id": session_id,
                    },
                )
                return
            self._open(task, "原来的 session 已关闭，没有别的可继续，所以新开一个。")
            return
        self._session_event("continued", record, reason)
        try:
            self.backend.run(
                record.backend_session_id,
                task.text,
                audi_session_id=record.session_id,
            )
        except BackendNotConfigured as exc:
            self._not_configured(task.message_id, str(exc))

    def _not_configured(self, message_id: str, text: str) -> None:
        spoken = "DeepSeek 还没配好密钥，现在接不上。"
        self.events.append(
            type="error",
            source="dispatch",
            ref=message_id,
            data={"reason": text},
        )
        if self.outbox is None:
            return
        self.outbox.enqueue(
            {
                "kind": "error",
                "source_agent": "deepseek",
                "source_label": "DeepSeek",
                "status": "failed",
                "text": text,
                "spoken": spoken,
                "spoken_text": spoken,
            }
        )

    def _decide(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            self.events.append(
                type="error",
                source="dispatch",
                ref=_message_id(message),
                data={"reason": "decision 缺少 request_id"},
            )
            return
        submit = getattr(self.backend, "submit_decision", None)
        if submit is None:
            self.events.append(
                type="error",
                source="dispatch",
                ref=request_id,
                data={"reason": "当前 backend 不接收审批"},
            )
            return
        answers = message.get("answers") if isinstance(message.get("answers"), list) else None
        decision = message.get("decision") if isinstance(message.get("decision"), str) else None
        note = message.get("note") if isinstance(message.get("note"), str) else None
        if note is None and isinstance(message.get("text"), str):
            note = message.get("text")
        submit(request_id.strip(), decision, answers, note)

    def _close(self, message: Mapping[str, Any]) -> None:
        session_id = message.get("session_id")
        message_id = message.get("message_id") if isinstance(message.get("message_id"), str) else None
        if not isinstance(session_id, str) or not session_id.strip():
            self.events.append(
                type="error",
                source="sessions",
                ref=message_id,
                data={"reason": "close_session 缺少 session_id"},
            )
            return
        try:
            record = self.sessions.close(session_id.strip())
        except SessionNotFound:
            self.events.append(
                type="error",
                source="sessions",
                ref=message_id,
                data={"reason": "session 不存在", "session_id": session_id.strip()},
            )
            return
        self._session_event("closed", record, "关闭 session")

    def _session_event(self, action: str, record: Session, reason: str) -> None:
        self.events.append(
            type="session",
            source="sessions",
            ref=record.session_id,
            data={
                "action": action,
                "session_id": record.session_id,
                "status": record.status,
                "backend_session_id": record.backend_session_id,
                "title": record.title,
                "pending_questions": record.pending_questions,
                "reason": reason,
            },
        )


def _message_id(message: Mapping[str, Any]) -> str | None:
    raw = message.get("message_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _task_message(message: Mapping[str, Any]) -> TaskMessage:
    session_id = message.get("session_id")
    target = message.get("target")
    return TaskMessage(
        message_id=str(message.get("message_id") or ""),
        kind="task",
        text=str(message.get("text") or ""),
        session_id=session_id.strip() if isinstance(session_id, str) and session_id.strip() else None,
        target=target.strip() if isinstance(target, str) and target.strip() else None,
    )
