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

from audi28.approval import ApprovalCenter
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
        pending: ApprovalCenter,
        harness_factory: Callable[..., Any] | None = None,
        sessions: Any = None,
        dsh_bin: str | None = None,
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
        if hasattr(self.pending, "bind"):
            self.pending.bind(events=events, outbox=outbox)
        self.sessions = sessions
        self.dsh_bin = dsh_bin
        self._audi_by_backend: dict[str, str] = {}
        self._factory = harness_factory or _real_harness
        self._harness: Any = None
        self._harness_lock = threading.Lock()
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

    def run(self, backend_session_id: str, text: str, *, audi_session_id: str | None = None) -> None:
        audi_id = audi_session_id or backend_session_id
        self._require_key(audi_id)
        self._submit(backend_session_id, audi_id, text, "继续处理这条任务。")

    def submit_decision(
        self,
        request_id: str,
        decision: str | None,
        answers: list[dict[str, Any]] | None = None,
        message: str | None = None,
    ) -> None:
        result = self.pending.decide(request_id, decision, answers, message=message, source="user")
        if result.applied and result.decision is not None:
            self._wake(request_id, decision, answers)
            self.pending.mark_delivered(result.decision.id)
        return result

    def deliver_decisions(self) -> None:
        for made in self.pending.undelivered():
            if made.request_id not in self._waiters:
                continue
            self._wake(made.request_id, made.decision, made.answers)
            self.pending.mark_delivered(made.id)

    def _wake(self, request_id: str, decision: str | None, answers: list[dict[str, Any]] | None) -> None:
        waiter = self._waiters.get(request_id)
        if waiter is None:
            return
        waiter.decision = decision
        waiter.answers = answers
        waiter.event.set()

    def close(self) -> None:
        self._stop.set()
        for waiter in list(self._waiters.values()):
            waiter.event.set()
        for jobs in list(self._queues.values()):
            jobs.put(None)
        try:
            harness = self._harness
            if harness is not None and hasattr(harness, "close"):
                harness.close()
        finally:
            for worker in list(self._workers.values()):
                worker.join(timeout=1)
            pump = self._pump
            if pump is not None:
                pump.join(timeout=1)

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
        with self._harness_lock:
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
            dsh_bin=self.dsh_bin,
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
        self._audi_by_backend[handle] = audi_session_id
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
                        audi_session_id,
                        f"恢复 DeepSeek session 失败，没有另建一个。{exc.__class__.__name__}",
                        "DeepSeek 没能接着上次的会话，没有另开一个。",
                        backend_session_id=handle,
                    )
                    continue
                self._sessions[handle] = session
            try:
                self._apply_permission(session)
                self._say(audi_session_id, progress, kind="progress", backend_session_id=handle)
                result = session.run(text)
            except Exception as exc:
                self._report(
                    "error",
                    audi_session_id,
                    f"DeepSeek 这一轮失败了。{exc.__class__.__name__}",
                    "DeepSeek 这一轮没做完。",
                    backend_session_id=handle,
                )
                continue
            self._say(audi_session_id, _final_text(result), kind="result", backend_session_id=handle)

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
        backend_sid = _text(payload.get("sessionId"))
        record = self.pending.create(
            kind="approval",
            session_id=self._audi_by_backend.get(backend_sid, backend_sid),
            backend_session_id=backend_sid,
            rpc_id=str(request.id),
            tool_name=tool,
            call_id=_text(payload.get("callId")) or None,
            reason=reason or None,
            command=command or None,
            text=_approval_text(tool, command, reason),
            spoken=_approval_spoken(tool),
        )
        if record.status == "decided":
            client.respond(request.id, {"outcome": _approval_outcome(record.decision)})
            return
        waiter = self._waiters.get(record.id)
        if waiter is None:
            waiter = _Waiter(threading.Event())
            self._waiters[record.id] = waiter
        while not waiter.event.wait(timeout=0.2):
            if self._stop.is_set():
                break
        if self._stop.is_set() and waiter.decision is None:
            client.respond(request.id, {"outcome": "cancelled"})
            return
        outcome = _approval_outcome(waiter.decision)
        client.respond(request.id, {"outcome": outcome})

    def _on_user_question(self, request: Any, cancelled: threading.Event) -> dict[str, Any]:
        questions = list(getattr(request, "questions", []) or [])
        preview = _text(questions[0].get("question")) if questions and isinstance(questions[0], dict) else ""
        backend_sid = str(getattr(request, "session_id", "") or "")
        record = self.pending.create(
            kind="user_question",
            session_id=self._audi_by_backend.get(backend_sid, backend_sid),
            backend_session_id=backend_sid,
            rpc_id=str(getattr(request, "request_id", "") or ""),
            questions=questions,
            reason=preview or None,
            text=_question_text(preview),
            spoken=_question_spoken(preview),
        )
        if record.status == "decided":
            if record.decision in {None, "deny", "cancel"}:
                return {"cancelled": True}
            return {"answers": _normalize_answers(record.answers)}
        waiter = self._waiters.get(record.id)
        if waiter is None:
            waiter = _Waiter(threading.Event())
            self._waiters[record.id] = waiter
        while not waiter.event.wait(timeout=0.2):
            if cancelled.is_set() or self._stop.is_set():
                return {"cancelled": True}
        if waiter.decision in {None, "deny", "cancel"}:
            return {"cancelled": True}
        return {"answers": _normalize_answers(waiter.answers)}

    def _say(self, session_id: str, message: str, *, kind: str, backend_session_id: str | None = None) -> None:
        spoken = _voice(message)
        self.events.append(
            type="reply" if kind == "result" else "task",
            source="deepseek",
            ref=session_id,
            data={
                "kind": kind,
                "text": message,
                "session_id": session_id,
                "backend_session_id": backend_session_id,
            },
        )
        self.outbox.enqueue(
            {
                "kind": "result" if kind == "result" else "progress",
                "session_id": session_id,
                "backend_session_id": backend_session_id,
                "source_agent": "deepseek",
                "source_label": "DeepSeek",
                "status": "done" if kind == "result" else "running",
                "text": message,
                "spoken": spoken,
                "spoken_text": spoken,
            }
        )

    def _report(
        self,
        kind: str,
        ref: str,
        text: str,
        spoken: str,
        *,
        backend_session_id: str | None = None,
    ) -> None:
        self.events.append(
            type="error",
            source="deepseek",
            ref=ref,
            data={"reason": text, "session_id": ref, "backend_session_id": backend_session_id},
        )
        self.outbox.enqueue(
            {
                "kind": kind,
                "session_id": ref,
                "backend_session_id": backend_session_id,
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
            dsh_bin=options.get("dsh_bin"),
        )
    )


def _normalize_answers(answers: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in answers or []:
        if not isinstance(item, dict):
            continue
        selected = item.get("selected")
        if isinstance(selected, str):
            labels = [selected] if selected.strip() else []
        elif isinstance(selected, list):
            labels = [part.strip() for part in selected if isinstance(part, str) and part.strip()]
        else:
            labels = []
        entry: dict[str, Any] = {"id": _text(item.get("id")), "selected": labels}
        custom = item.get("custom")
        if isinstance(custom, str) and custom.strip():
            entry["custom"] = custom
        normalized.append(entry)
    return normalized


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
    cleaned = _strip_markdown(text)
    cleaned = _PATHISH.sub("", cleaned)
    return " ".join(cleaned.split())


def _strip_markdown(text: str) -> str:
    without_fences = re.sub(r"```[A-Za-z0-9_-]*", "", text).replace("```", "")
    without_links = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", without_fences)
    without_bold = re.sub(r"\*\*([^*]+)\*\*", r"\1", without_links)
    without_bold = re.sub(r"__([^_]+)__", r"\1", without_bold)
    without_em = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", without_bold)
    return without_em.replace("`", "")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
