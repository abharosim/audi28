"""Pluggable routing. Phase 1 ships the rules implementation.

An LLM judge can implement the same `decide` method later. If it times out
or cannot be parsed, call `RulesRouter` instead of inventing a second policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

_CONTINUABLE = {"open", "idle", "running"}


@dataclass(frozen=True)
class TaskMessage:
    message_id: str
    kind: str
    text: str
    session_id: str | None
    target: str | None


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    status: str
    updated_ts: str


@dataclass(frozen=True)
class RouteContext:
    sessions: tuple[SessionSnapshot, ...]


@dataclass(frozen=True)
class RouteDecision:
    target: str
    session_action: str
    session_id: str | None
    reason: str
    rule_id: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "target": self.target,
            "session_action": self.session_action,
            "session_id": self.session_id,
            "reason": self.reason,
            "rule_id": self.rule_id,
        }


class Router(Protocol):
    def decide(self, message: TaskMessage, context: RouteContext) -> RouteDecision:
        """Choose a target and whether to continue a session or open one."""


class RulesRouter:
    def __init__(
        self,
        *,
        default: str,
        new_task_phrases: tuple[str, ...],
        not_new_task_phrases: tuple[str, ...],
        self_phrases: tuple[str, ...],
    ) -> None:
        self._default = "audi_self" if default == "self" else default
        self._new_task_phrases = new_task_phrases
        self._not_new_task_phrases = not_new_task_phrases
        self._self_phrases = self_phrases

    def decide(self, message: TaskMessage, context: RouteContext) -> RouteDecision:
        target, target_rule, target_reason = self._target(message)
        if target == "audi_self":
            return RouteDecision(
                target="audi_self",
                session_action="none",
                session_id=None,
                reason=target_reason,
                rule_id=target_rule,
            )
        action, session_id, reason, rule_id = self._session(message, context)
        if target_rule == "explicit-target":
            reason = f"{target_reason} {reason}"
        return RouteDecision(
            target="deepseek",
            session_action=action,
            session_id=session_id,
            reason=reason,
            rule_id=rule_id,
        )

    def _target(self, message: TaskMessage) -> tuple[str, str, str]:
        requested = (message.target or "").strip()
        if requested in {"deepseek", "audi_self", "self"}:
            target = "audi_self" if requested in {"self", "audi_self"} else "deepseek"
            return target, "explicit-target", f"消息指定了 target {target}。"
        if _first_hit(message.text, self._self_phrases, ()):
            return "audi_self", "self-phrase", "这句话是让 Audi 自己回答，不交给 DeepSeek。"
        if self._default == "audi_self":
            return "audi_self", "default", "默认不交给 DeepSeek。"
        return "deepseek", "default", "已经进入 inbox，按默认交给 DeepSeek。"

    def _session(
        self,
        message: TaskMessage,
        context: RouteContext,
    ) -> tuple[str, str | None, str, str]:
        named = (message.session_id or "").strip()
        if named:
            return (
                "continue",
                named,
                "消息指定了 session，这条优先于新任务说法。",
                "explicit-session",
            )
        hit = _first_hit(message.text, self._new_task_phrases, self._not_new_task_phrases)
        if hit:
            return "new", None, f"正文明确要新任务，命中「{hit}」。", "new-task"
        latest = _latest_continuable(context.sessions)
        if latest is not None:
            return (
                "continue",
                latest.session_id,
                "没有新任务说法，继续最近一个仍开放的 session。",
                "continue-latest",
            )
        return "new", None, "没有仍开放的 session，所以新开一个。", "new-because-none"


def _latest_continuable(sessions: tuple[SessionSnapshot, ...]) -> SessionSnapshot | None:
    continuable = [session for session in sessions if session.status in _CONTINUABLE]
    if not continuable:
        return None
    return max(continuable, key=lambda session: session.updated_ts)


def _first_hit(text: str, phrases: tuple[str, ...], negations: tuple[str, ...]) -> str | None:
    folded = text.casefold()
    covered = _spans(folded, negations)
    for phrase in phrases:
        needle = phrase.casefold()
        if not needle:
            continue
        start = 0
        while True:
            found = folded.find(needle, start)
            if found < 0:
                break
            end = found + len(needle)
            if not any(neg_start <= found and end <= neg_end for neg_start, neg_end in covered):
                return phrase
            start = found + 1
    return None


def _spans(folded: str, phrases: tuple[str, ...]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for phrase in phrases:
        needle = phrase.casefold()
        if not needle:
            continue
        start = 0
        while True:
            found = folded.find(needle, start)
            if found < 0:
                break
            spans.append((found, found + len(needle)))
            start = found + 1
    return spans
