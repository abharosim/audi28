"""Command line entry point for the audi28 skeleton."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from audi28.config import ConfigError, prepare
from audi28.doctor import doctor_report
from audi28.events import EventLog, EventLogError
from audi28.supervisor import supervise


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


def _replay(directory: Path) -> int:
    try:
        events = EventLog(directory).replay()
    except EventLogError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for event in events:
        print(json.dumps(event.as_dict(), ensure_ascii=False))
    return 0
