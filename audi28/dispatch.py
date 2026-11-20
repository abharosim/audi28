"""Turn one inbox message into a route event, a session event, then a backend call."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from audi28.backend import Backend
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
    ) -> None:
        self.router = router
        self.sessions = sessions
        self.backend = backend
        self.events = events
        self.workspace = workspace

    def handle(self, message: Mapping[str, Any]) -> None:
        kind = message.get("kind")
        if kind == "close_session":
            self._close(message)
            return
        if kind != "task":
            return
        task = _task_message(message)
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
            self._continue(task, decision.session_id)
            return
        self._open(task)

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

    def _open(self, task: TaskMessage) -> None:
        record = self.sessions.create(
            title=task.text,
            backend="deepseek",
            backend_session_id="",
            workspace=self.workspace,
            task_id=task.message_id,
        )
        self._session_event("created", record)
        handle = self.backend.start(record.session_id, task.text)
        if handle:
            record = self.sessions.set_handle(record.session_id, handle)

    def _continue(self, task: TaskMessage, session_id: str) -> None:
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
        self._session_event("continued", record)
        self.backend.run(record.backend_session_id, task.text)

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
        self._session_event("closed", record)

    def _session_event(self, action: str, record: Session) -> None:
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
            },
        )


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
