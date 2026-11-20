"""DeepSeek Harness adapter. The SDK is imported only when a key is present.

Pinned SDK: deepseek-harness-sdk==0.2.2rc2. That release has no permission_mode
argument. The workspace is `cwd`. The preset is applied with
`Session.run("/permission <mode>")` after start_session and resume_session.
"""

from __future__ import annotations

import queue
import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from audi28.approvals import PendingStore
from audi28.backend import BackendNotConfigured
from audi28.events import EventLog

_PATHISH = re.compile(r"(https?://\S+|~?/[\w./~-]+|[A-Za-z]:\\[\w.\\ -]+|sk-[A-Za-z0-9_-]+)")


@dataclass
class _Waiter:
    event: threading.Event
    decision: str | None = None
    answers: list[dict[str, Any]] | None = None


class DeepSeekBackend:
    def __init__(
        self,
        *,
        api_key: str,
        workspace: Path,
        dsh_home: Path,
        permission_mode: str,
        provider: str,
        model: str,
        profile: str,
        initialize_timeout_seconds: float,
        outbox: Any,
        events: EventLog,
        pending: PendingStore,
        harness_factory: Callable[..., Any] | None = None,
        sessions: Any = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.workspace = Path(workspace)
        self.dsh_home = Path(dsh_home)
        self.permission_mode = permission_mode
        self.provider = provider
        self.model = model
        self.profile = profile
        self.initialize_timeout_seconds = initialize_timeout_seconds
        self.outbox = outbox
        self.events = events
        self.pending = pending
        self.sessions = sessions
        self._factory = harness_factory or _real_harness
        self._harness: Any = None
        self._sessions: dict[str, Any] = {}
        self._queues: dict[str, queue.Queue] = {}
        self._workers: dict[str, threading.Thread] = {}
        self._worker_guard = threading.Lock()
        self._permitted: set[str] = set()
        self._waiters: dict[str, _Waiter] = {}
        self._pump: threading.Thread | None = None
        self._stop = threading.Event()
        self.configured = bool(self.api_key)

    def start(self, session_id: str, text: str) -> str:
        self._require_key(session_id)
        handle = f"session-{uuid.uuid4().hex}"
        session = self._open_harness().start_session(handle)
        self._sessions[handle] = session
        if self.sessions is not None:
            self.sessions.set_handle(session_id, handle)
        self._submit(handle, session_id, text, "开始处理这条任务。")
        return handle

    def run(self, backend_session_id: str, text: str) -> None:
        self._require_key(backend_session_id)
        self._submit(backend_session_id, backend_session_id, text, "继续处理这条任务。")

    def submit_decision(
        self,
        request_id: str,
        decision: str | None,
        answers: list[dict[str, Any]] | None = None,
    ) -> None:
        record = self.pending.resolve(request_id, decision, answers)
        if record is None:
            self.events.append(
                type="error",
                source="deepseek",
                ref=request_id,
                data={"reason": "没有这条待处理的提问"},
            )
            return
        self.events.append(
            type="decision",
            source="deepseek",
            ref=request_id,
            data={"decision": decision, "status": "decided"},
        )
        waiter = self._waiters.get(request_id)
        if waiter is not None:
            waiter.decision = decision
            waiter.answers = answers
            waiter.event.set()

    def close(self) -> None:
        self._stop.set()
        for waiter in list(self._waiters.values()):
            waiter.event.set()
        harness = self._harness
        if harness is not None and hasattr(harness, "close"):
            harness.close()

    def _require_key(self, ref: str) -> None:
        if self.api_key:
            return
        self.events.append(
            type="error",
            source="deepseek",
            ref=ref,
            data={"reason": "DEEPSEEK_API_KEY 未配置"},
        )
        raise BackendNotConfigured(
            "没有找到 DEEPSEEK_API_KEY。把密钥写进项目的 .env，或导出到环境变量后再试。"
        )

    def _open_harness(self) -> Any:
        if self._harness is not None:
            return self._harness
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.dsh_home.mkdir(parents=True, exist_ok=True)
        harness = self._factory(
            cwd=str(self.workspace),
            dsh_home=str(self.dsh_home),
            api_key=self.api_key,
            provider=self.provider,
            model=self.model,
            profile=self.profile,
            approval_requests=True,
            initialize_timeout_seconds=self.initialize_timeout_seconds,
            on_user_question=self._on_user_question,
            permission_mode=self.permission_mode,
        )
        self._harness = harness
        self._start_pump(harness)
        return harness

    def _start_pump(self, harness: Any) -> None:
        client = getattr(harness, "client", harness)

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    request = client.next_request()
                except Exception:
                    return
                if request is None:
                    return
                if getattr(request, "method", "") != "approval/request":
                    if hasattr(client, "respond_error"):
                        try:
                            client.respond_error(
                                request.id,
                                code=-32601,
                                message="Method not found",
                            )
                        except Exception:
                            return
                    continue
                self._handle_approval(client, request)

        self._pump = threading.Thread(target=loop, name="audi28-deepseek-approvals", daemon=True)
        self._pump.start()

    def _submit(self, handle: str, audi_session_id: str, text: str, progress: str) -> None:
        jobs = self._queues.setdefault(handle, queue.Queue())
        jobs.put((audi_session_id, text, progress))
        with self._worker_guard:
            if handle in self._workers:
                return
            worker = threading.Thread(
                target=self._session_loop,
                args=(handle,),
                name=f"audi28-deepseek-{handle[-8:]}",
                daemon=True,
            )
            self._workers[handle] = worker
            worker.start()

    def _session_loop(self, handle: str) -> None:
        jobs = self._queues[handle]
        while not self._stop.is_set():
            try:
                item = jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            audi_session_id, text, progress = item
            session = self._sessions.get(handle)
            if session is None:
                try:
                    session = self._open_harness().resume_session(handle)
                except Exception as exc:
                    self._report(
                        "error",
                        handle,
                        f"恢复 DeepSeek session 失败，没有另建一个。{exc.__class__.__name__}",
                        "DeepSeek 没能接着上次的会话，没有另开一个。",
                    )
                    continue
                self._sessions[handle] = session
            self._apply_permission(session)
            self._say(audi_session_id, progress, kind="progress")
            result = session.run(text)
            self._say(audi_session_id, _final_text(result), kind="result")

    def _apply_permission(self, session: Any) -> None:
        if session.id in self._permitted:
            return
        session.run(f"/permission {self.permission_mode}")
        self._permitted.add(session.id)

    def _handle_approval(self, client: Any, request: Any) -> None:
        payload = request.payload if isinstance(request.payload, dict) else {}
        tool = _text(payload.get("toolName"))
        reason = _text(payload.get("reason"))
        command = _text(payload.get("command") or payload.get("path") or payload.get("cwd"))
        record = self.pending.create(
            kind="approval",
            session_id=_text(payload.get("sessionId")),
            backend_session_id=_text(payload.get("sessionId")),
            rpc_id=str(request.id),
            tool_name=tool,
            call_id=_text(payload.get("callId")) or None,
            reason=reason or None,
            command=command or None,
        )
        self.events.append(
            type="approval",
            source="deepseek",
            ref=record.id,
            data={"kind": "approval", "tool_name": tool, "status": "pending"},
        )
        self._ask("approval", record.id, _approval_text(tool, command, reason), _approval_spoken(tool))
        waiter = _Waiter(threading.Event())
        self._waiters[record.id] = waiter
        waiter.event.wait()
        outcome = _approval_outcome(waiter.decision)
        client.respond(request.id, {"outcome": outcome})

    def _on_user_question(self, request: Any, cancelled: threading.Event) -> dict[str, Any]:
        questions = list(getattr(request, "questions", []) or [])
        record = self.pending.create(
            kind="user_question",
            session_id=str(getattr(request, "session_id", "") or ""),
            backend_session_id=str(getattr(request, "session_id", "") or ""),
            rpc_id=str(getattr(request, "request_id", "") or ""),
            questions=questions,
        )
        preview = _text(questions[0].get("question")) if questions and isinstance(questions[0], dict) else ""
        self.events.append(
            type="approval",
            source="deepseek",
            ref=record.id,
            data={"kind": "user_question", "status": "pending"},
        )
        self._ask("user_question", record.id, _question_text(preview), _question_spoken(preview))
        waiter = _Waiter(threading.Event())
        self._waiters[record.id] = waiter
        while not waiter.event.wait(timeout=0.2):
            if cancelled.is_set():
                return {"cancelled": True}
        if waiter.decision in {None, "deny", "cancel"}:
            return {"cancelled": True}
        return {"answers": waiter.answers or []}

    def _ask(self, kind: str, request_id: str, text: str, spoken: str) -> None:
        self.outbox.enqueue(
            {
                "kind": kind,
                "request_id": request_id,
                "source_agent": "deepseek",
                "source_label": "DeepSeek",
                "status": "needs_decision",
                "text": text,
                "spoken": spoken,
                "spoken_text": spoken,
            }
        )

    def _say(self, session_id: str, message: str, *, kind: str) -> None:
        spoken = _voice(message)
        self.events.append(
            type="reply" if kind == "result" else "task",
            source="deepseek",
            ref=session_id,
            data={"kind": kind, "text": message},
        )
        self.outbox.enqueue(
            {
                "kind": "result" if kind == "result" else "progress",
                "session_id": session_id,
                "source_agent": "deepseek",
                "source_label": "DeepSeek",
                "status": "done" if kind == "result" else "running",
                "text": message,
                "spoken": spoken,
                "spoken_text": spoken,
            }
        )

    def _report(self, kind: str, ref: str, text: str, spoken: str) -> None:
        self.events.append(type="error", source="deepseek", ref=ref, data={"reason": text})
        self.outbox.enqueue(
            {
                "kind": kind,
                "source_label": "DeepSeek",
                "status": "failed",
                "text": text,
                "spoken": spoken,
                "spoken_text": spoken,
            }
        )


def _real_harness(**options: Any) -> Any:
    from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

    return DeepSeekHarness(
        DeepSeekHarnessConfig(
            cwd=options["cwd"],
            dsh_home=options["dsh_home"],
            api_key=options["api_key"],
            provider=options["provider"],
            model=options["model"],
            profile=options["profile"],
            approval_requests=True,
            initialize_timeout_seconds=options["initialize_timeout_seconds"],
            on_user_question=options["on_user_question"],
        )
    )


def _approval_outcome(decision: str | None) -> str:
    if decision == "allow":
        return "allowed-once"
    if decision == "deny":
        return "rejected"
    return "cancelled"


def _approval_text(tool: str, command: str, reason: str) -> str:
    parts = ["DeepSeek 在问你：是否允许它使用工具"]
    parts.append(tool or "一个工具")
    if command:
        parts.append(f"。命令或路径：{command}")
    if reason:
        parts.append(f"。原因：{reason}")
    parts.append("。只批准这一次。请回答允许或拒绝。")
    return "".join(parts)


def _approval_spoken(tool: str) -> str:
    if tool and not _PATHISH.search(tool):
        return f"DeepSeek 在问你：是否允许它使用工具 {tool}。只批准这一次。请回答允许或拒绝。"
    return "DeepSeek 在问你：是否允许它使用这个工具。只批准这一次。请回答允许或拒绝。"


def _question_text(preview: str) -> str:
    body = preview or "有一个问题要你回答"
    return f"DeepSeek 在问你：{body}"


def _question_spoken(preview: str) -> str:
    body = _voice(preview) or "有一个问题要你回答"
    return f"DeepSeek 在问你：{body}"


def _final_text(result: Any) -> str:
    text = getattr(result, "final_response", "") or ""
    text = str(text).strip() or "做完了，没有更多说明。"
    return f"DeepSeek 说：{text}"


def _voice(text: str) -> str:
    cleaned = _PATHISH.sub("", text)
    return " ".join(cleaned.split())


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
