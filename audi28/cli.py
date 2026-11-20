"""Command line entry point for the audi28 skeleton."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from audi28.config import Config, ConfigError, prepare
from audi28.doctor import doctor_report
from audi28.events import EventLog, EventLogError
from audi28.sessions import SessionNotFound, SessionStore
from audi28.supervisor import supervise
from audi28.transport import Outbox


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audi28")
    parser.add_argument("--config", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    events = sub.add_parser("events", help="read the JSONL event log")
    events_sub = events.add_subparsers(dest="events_command", required=True)
    events_sub.add_parser("replay", help="print stored events")

    sub.add_parser("supervise", help="run the local dashboard until signaled")
    sub.add_parser("web", help="run the local dashboard until signaled")
    sub.add_parser("doctor", help="check config and DEEPSEEK_API_KEY")
    grokbot = sub.add_parser("grokbot", help="talk to the Grok Bot webhook")
    grokbot_sub = grokbot.add_subparsers(dest="grokbot_command", required=True)
    grokbot_sub.add_parser("test", help="send one test message through the outbox")
    sessions = sub.add_parser("sessions", help="list or close audi28 sessions")
    sessions_sub = sessions.add_subparsers(dest="sessions_command", required=True)
    sessions_sub.add_parser("list", help="list sessions that are not closed")
    close = sessions_sub.add_parser("close", help="close one session")
    close.add_argument("session_id")

    args = parser.parse_args(_normalize_argv(argv))
    config_path = args.config or (Path.cwd() / "config.toml")

    if args.command == "doctor":
        report = doctor_report(config_path)
        sys.stdout.write(report.text)
        return report.exit_code

    try:
        config = prepare(config_path)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.command == "events":
        return _replay(config.root / "data" / "events")
    if args.command == "grokbot":
        return _grokbot_test(config)
    if args.command == "sessions":
        return _sessions_command(config, args.sessions_command, getattr(args, "session_id", None))
    supervise(config)
    return 0


def _normalize_argv(argv: list[str] | None) -> list[str] | None:
    """Allow ``--config`` before or after the subcommand.

    ``start.sh`` runs ``audi28 supervise --config <file>``. argparse only
    keeps parent options that appear before the subcommand, so move it up.
    """

    if argv is None:
        argv = sys.argv[1:]
    config: list[str] = []
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--config" and index + 1 < len(argv):
            config = ["--config", argv[index + 1]]
            index += 2
            continue
        if item.startswith("--config="):
            config = [item]
            index += 1
            continue
        rest.append(item)
        index += 1
    if not config:
        return rest
    return config + rest


def _sessions_command(config: Config, command: str, session_id: str | None) -> int:
    store = SessionStore(config.root / "data" / "sessions")
    if command == "list":
        for record in store.list():
            print(json.dumps(record.as_dict(), ensure_ascii=False))
        return 0
    if not session_id:
        print("要关闭的 session_id 不能空。", file=sys.stderr)
        return 1
    try:
        store.close(session_id)
    except SessionNotFound:
        print("没有这个 session。", file=sys.stderr)
        return 1
    print("已关闭。")
    return 0


def _grokbot_test(config: Config) -> int:
    url = os.environ.get("AUDI28_GROKBOT_WEBHOOK_URL", "").strip()
    key = os.environ.get("AUDI28_GROKBOT_WEBHOOK_KEY", "").strip()
    if not url or not key:
        print(
            "没有找到 AUDI28_GROKBOT_WEBHOOK_URL 或 AUDI28_GROKBOT_WEBHOOK_KEY。"
            "写进项目根目录的 .env，或导出到环境变量。"
        )
        return 1
    events = EventLog(config.root / "data" / "events")
    outbox = Outbox(
        config.root / "data" / "outbox" / "grokbot",
        events,
        url=url,
        key=key,
        header_name=config.grokbot.header_name,
        header_value=config.grokbot.header_value,
        timeout_s=config.grokbot.timeout_s,
        secrets=(url, key),
    )
    outbox.enqueue({"kind": "test", "text": "audi28 grokbot test"})
    outbox.flush_once()
    if list((outbox.root / "sent").glob("*.json")):
        print("webhook 测试已发送。")
        return 0
    print("webhook 测试没有发送成功。")
    return 1


def _replay(directory: Path) -> int:
    try:
        events = EventLog(directory).replay()
    except EventLogError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for event in events:
        print(json.dumps(event.as_dict(), ensure_ascii=False))
    return 0
