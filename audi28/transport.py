"""Inbox pickup and the Grok Bot webhook outbox.

Inbox files are claimed with rename. Outbound messages are written to disk
before any HTTP call, and a restart continues whatever is still pending.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from audi28.events import EventLog

_OPENER = urllib.request.urlopen
_INBOX_THREAD_LOCKS: dict[str, threading.Lock] = {}
_INBOX_THREAD_GUARD = threading.Lock()


class Inbox:
    def __init__(
        self,
        root: Path,
        events: EventLog,
        *,
        parse_retries: int,
        max_bytes: int,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.root = Path(root)
        self.processing = self.root / "processing"
        self.processed = self.root / "processed"
        self.quarantine = self.root / "quarantine"
        self._events = events
        self._parse_retries = parse_retries
        self._max_bytes = max_bytes
        self._secrets = tuple(secret for secret in secrets if secret)
        self._seen_ids, self._seen_keys = _seen_from_events(events)

    def poll_once(self) -> None:
        self._ensure_dirs()
        with _thread_lock(self.root), self._decision_lock():
            self._refresh_seen()
            leftovers = _json_files(self.processing)
            incoming = _json_files(self.root)
            for path in leftovers:
                self._accept_one(path)
            for path in incoming:
                self._accept_one(path)

    def _ensure_dirs(self) -> None:
        for directory in (self.root, self.processing, self.processed, self.quarantine):
            directory.mkdir(parents=True, exist_ok=True)

    def _accept_one(self, path: Path) -> None:
        claimed = self._claim(path)
        if claimed is None:
            return
        if claimed.is_symlink():
            self._quarantine(claimed, "symlink")
            return
        try:
            size = claimed.lstat().st_size
        except FileNotFoundError:
            return
        if size > self._max_bytes:
            self._quarantine(claimed, "too-large")
            return
        self._finish_one(claimed)

    def _claim(self, path: Path) -> Path | None:
        destination = self.processing / f"{uuid.uuid4().hex}-{path.name}"
        try:
            os.rename(path, destination)
        except FileNotFoundError:
            return None
        sidecar = _attempts_path(path)
        if sidecar.exists():
            try:
                os.rename(sidecar, _attempts_path(destination))
            except FileNotFoundError:
                pass
        return destination

    def _finish_one(self, path: Path) -> None:
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, FileNotFoundError):
            self._fail_parse(path)
            return
        message_id = obj.get("message_id") if isinstance(obj, dict) else None
        kind = obj.get("kind") if isinstance(obj, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            self._fail_parse(path)
            return
        if not isinstance(kind, str) or not kind.strip():
            self._fail_parse(path)
            return
        message_id = message_id.strip()
        idempotency_key = obj.get("idempotency_key") if isinstance(obj, dict) else None
        if isinstance(idempotency_key, str):
            idempotency_key = idempotency_key.strip() or None
        else:
            idempotency_key = None
        if message_id in self._seen_ids or (
            idempotency_key is not None and idempotency_key in self._seen_keys
        ):
            self._move(path, self.processed)
            self._error("duplicate message_id", ref=message_id)
            return
        self._events.append(
            type="inbox_received",
            source="inbox",
            ref=message_id,
            data={"message": _redact_value(obj, self._secrets)},
        )
        self._seen_ids.add(message_id)
        if idempotency_key is not None:
            self._seen_keys.add(idempotency_key)
        self._move(path, self.processed)

    def _fail_parse(self, path: Path) -> None:
        attempts = _read_attempts(path) + 1
        if attempts >= self._parse_retries:
            self._quarantine(path, "malformed")
            return
        _write_attempts(path, attempts)

    def _quarantine(self, path: Path, reason: str) -> None:
        if not path.exists() and not path.is_symlink():
            return
        self._move(path, self.quarantine)
        self._error(reason, ref=path.name)

    def _move(self, path: Path, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        destination = _unique_destination(directory, path.name)
        try:
            os.rename(path, destination)
        except FileNotFoundError:
            return
        attempts = _attempts_path(path)
        if attempts.exists():
            attempts.unlink()

    def _refresh_seen(self) -> None:
        ids, keys = _seen_from_events(self._events)
        self._seen_ids.update(ids)
        self._seen_keys.update(keys)

    def _decision_lock(self):
        return _exclusive_lock(self.root)

    def _error(self, reason: str, *, ref: str | None) -> None:
        self._events.append(
            type="error",
            source="inbox",
            ref=ref,
            data={"reason": reason},
        )


class Outbox:
    def __init__(
        self,
        root: Path,
        events: EventLog,
        *,
        url: str | None,
        key: str | None,
        header_name: str = "Authorization",
        header_value: str = "Bearer {key}",
        timeout_s: float = 10,
        backoff_s: float = 0.5,
        secrets: tuple[str, ...] = (),
        sleeper: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = _OPENER,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root)
        self.pending = self.root / "pending"
        self.inflight = self.root / "inflight"
        self.sent = self.root / "sent"
        self.quarantine = self.root / "quarantine"
        self._events = events
        self.url = url.strip() if isinstance(url, str) else ""
        self.key = key.strip() if isinstance(key, str) else ""
        self.header_name = header_name
        self.header_value = header_value
        self.timeout_s = timeout_s
        self.backoff_s = backoff_s
        self._secrets = tuple(
            secret for secret in (*secrets, self.url, self.key) if secret
        )
        self._sleep = sleeper
        self._open = opener
        self._clock = clock or (lambda: datetime.now().astimezone())

    def enqueue(self, payload: Mapping[str, Any]) -> str:
        if not isinstance(payload, Mapping):
            raise TypeError("webhook payload 必须是对象。")
        self._ensure_dirs()
        item_id = f"out-{uuid.uuid4().hex}"
        item = {"id": item_id, "payload": dict(payload), "attempts": 0}
        _atomic_json(self.pending / f"{item_id}.json", item)
        self._events.append(
            type="webhook",
            source="webhook",
            ref=item_id,
            data={"status": "queued"},
        )
        return item_id

    def flush_once(self) -> None:
        self._ensure_dirs()
        with _thread_lock(self.root), _exclusive_lock(self.root):
            self._recover_inflight()
            for path in _json_files(self.pending):
                self._flush_one(path)

    def _flush_one(self, path: Path) -> None:
        item = self._load_item(path)
        if item is None:
            return
        if not self._credentials_ok():
            return
        if not _due(item.get("next_attempt_ts"), self._clock):
            return
        claimed = self.inflight / path.name
        try:
            os.rename(path, claimed)
        except FileNotFoundError:
            return
        item["owner_pid"] = os.getpid()
        try:
            _atomic_json(claimed, item)
        except OSError:
            return
        reason = self._deliver(item["payload"])
        ref = str(item.get("id") or path.stem)
        if reason is None:
            try:
                os.rename(claimed, _unique_destination(self.sent, path.name))
            except FileNotFoundError:
                pass
            self._events.append(
                type="webhook",
                source="webhook",
                ref=ref,
                data={"status": "sent"},
            )
            return
        extra = 2 if reason in {"timeout", "network"} else 1
        item["attempts"] = int(item.get("attempts") or 0) + extra
        item.pop("owner_pid", None)
        delay = self.backoff_s * (2 ** min(int(item["attempts"]), 5))
        item["next_attempt_ts"] = (self._clock() + timedelta(seconds=delay)).isoformat()
        try:
            _atomic_json(self.pending / path.name, item)
        except OSError:
            return
        try:
            if claimed.exists():
                claimed.unlink()
        except FileNotFoundError:
            pass
        self._events.append(
            type="error",
            source="webhook",
            ref=ref,
            data={"reason": _redact_value(reason, self._secrets), "status": "error"},
        )

    def _load_item(self, path: Path) -> dict[str, Any] | None:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError):
            self._quarantine_outbox(path, "malformed")
            return None
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
            self._quarantine_outbox(path, "malformed")
            return None
        return item

    def _quarantine_outbox(self, path: Path, reason: str) -> None:
        self.quarantine.mkdir(parents=True, exist_ok=True)
        destination = _unique_destination(self.quarantine, path.name)
        try:
            os.rename(path, destination)
        except FileNotFoundError:
            return
        self._events.append(
            type="error",
            source="webhook",
            ref=path.stem,
            data={"reason": reason},
        )

    def _deliver(self, payload: Mapping[str, Any]) -> str | None:
        reason = self._post(payload)
        if reason in {"timeout", "network"}:
            self._sleep(self.backoff_s)
            reason = self._post(payload)
        return reason

    def _post(self, payload: Mapping[str, Any]) -> str | None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = self.header_value.replace("{key}", self.key)
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                self.header_name: header,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(request, timeout=self.timeout_s) as response:
                response.read()
                status = int(getattr(response, "status", 0) or 0)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
        except urllib.error.URLError as exc:
            if _is_timeout(exc) or _is_timeout(getattr(exc, "reason", None)):
                return "timeout"
            return "network"
        except (TimeoutError, socket.timeout):
            return "timeout"
        if 200 <= status < 300:
            return None
        return f"http {status}"

    def _recover_inflight(self) -> None:
        me = os.getpid()
        for path in _json_files(self.inflight):
            owner = _owner_pid(path)
            if owner is not None and owner != me and _pid_alive(owner):
                continue
            destination = self.pending / path.name
            if destination.exists():
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                continue
            try:
                os.rename(path, destination)
            except FileNotFoundError:
                continue

    def _ensure_dirs(self) -> None:
        for directory in (self.pending, self.inflight, self.sent, self.quarantine):
            directory.mkdir(parents=True, exist_ok=True)

    def _credentials_ok(self) -> bool:
        if not self.url or not self.key:
            return False
        if any(char in self.url or char in self.key for char in "\r\n"):
            return False
        return True


def _thread_lock(root: Path) -> threading.Lock:
    key = str(root.resolve())
    with _INBOX_THREAD_GUARD:
        lock = _INBOX_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _INBOX_THREAD_LOCKS[key] = lock
        return lock


def _exclusive_lock(directory: Path) -> _LockedFd:
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return _LockedFd(fd)


def _owner_pid(path: Path) -> int | None:
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(item, dict):
        return None
    owner = item.get("owner_pid")
    if isinstance(owner, bool) or not isinstance(owner, int):
        return None
    return owner


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class _LockedFd:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def __enter__(self) -> "_LockedFd":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)


def _seen_from_events(events: EventLog) -> tuple[set[str], set[str]]:
    ids: set[str] = set()
    keys: set[str] = set()
    for event in events.replay():
        if event.type != "inbox_received":
            continue
        if isinstance(event.ref, str) and event.ref:
            ids.add(event.ref)
        message = event.data.get("message") if isinstance(event.data, dict) else None
        if not isinstance(message, dict):
            continue
        key = message.get("idempotency_key")
        if isinstance(key, str) and key.strip():
            keys.add(key.strip())
    return ids, keys


def _json_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    found: list[Path] = []
    for entry in os.scandir(directory):
        name = entry.name
        if name.startswith(".") or not name.endswith(".json"):
            continue
        if entry.is_dir(follow_symlinks=False):
            continue
        found.append(directory / name)
    return sorted(found)


def _attempts_path(path: Path) -> Path:
    return path.parent / f"{path.name}.attempts"


def _read_attempts(path: Path) -> int:
    sidecar = _attempts_path(path)
    try:
        return int(sidecar.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def _write_attempts(path: Path, attempts: int) -> None:
    sidecar = _attempts_path(path)
    sidecar.write_text(f"{attempts}\n", encoding="utf-8")


def _unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem = Path(name).stem
    suffix = Path(name).suffix
    number = 2
    while True:
        candidate = directory / f"{stem}-{number}{suffix}"
        if not candidate.exists():
            return candidate
        number += 1


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


def _due(next_attempt: object, clock: Callable[[], datetime]) -> bool:
    if not isinstance(next_attempt, str) or not next_attempt:
        return True
    try:
        moment = datetime.fromisoformat(next_attempt)
    except ValueError:
        return True
    now = clock()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=now.tzinfo)
    return moment <= now


def _is_timeout(exc: object) -> bool:
    return isinstance(exc, (TimeoutError, socket.timeout))


def _redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "[redacted]")
        return redacted
    if isinstance(value, list):
        return [_redact_value(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, secrets) for key, item in value.items()}
    return value
