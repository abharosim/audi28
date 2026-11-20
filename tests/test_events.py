import threading
from datetime import date, datetime, timezone

import pytest

from audi28.events import EventLog, EventLogError


def test_append_and_replay_round_trip(tmp_path):
    moment = datetime(2026, 11, 20, 6, 7, 8, 9000, tzinfo=timezone.utc)
    log = EventLog(tmp_path, clock=lambda: moment, id_factory=lambda: "evt-fixed")

    event = log.append(type="task", source="test", ref="msg-1", data={"text": "跑测试"})

    assert event.id == "evt-fixed"
    assert event.ts == "2026-11-20T06:07:08.009+00:00"
    assert event.type == "task"
    assert event.source == "test"
    assert event.ref == "msg-1"
    assert event.data == {"text": "跑测试"}
    assert log.replay() == [event]
    raw = (tmp_path / "2026-11-20.jsonl").read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert "跑测试" in raw


def test_unknown_type_is_kept(tmp_path):
    log = EventLog(tmp_path)

    log.append(type="made-up-type", source="test", data={})

    assert log.replay()[0].type == "made-up-type"


def test_partial_tail_is_skipped(tmp_path):
    moment = datetime(2026, 11, 20, tzinfo=timezone.utc)
    log = EventLog(tmp_path, clock=lambda: moment, id_factory=lambda: "evt-1")
    log.append(type="task", source="test", data={"n": 1})
    path = tmp_path / "2026-11-20.jsonl"
    with path.open("ab") as fh:
        fh.write(b'{"id":"evt-partial"')

    assert [event.id for event in log.replay()] == ["evt-1"]


def test_file_of_only_a_partial_line_replays_empty(tmp_path):
    (tmp_path / "2026-11-20.jsonl").write_bytes(b'{"id":"evt-partial"')
    log = EventLog(tmp_path)

    assert log.replay() == []


def test_append_after_partial_tail_does_not_merge(tmp_path):
    moment = datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc)
    ids = iter(["evt-1", "evt-2"])
    log = EventLog(tmp_path, clock=lambda: moment, id_factory=lambda: next(ids))
    log.append(type="task", source="test", data={})
    path = tmp_path / "2026-11-20.jsonl"
    with path.open("ab") as fh:
        fh.write(b'{"id":"evt-partial"')

    log.append(type="after", source="test", data={})

    body = path.read_text(encoding="utf-8")
    assert "evt-partial" not in body
    assert [event.type for event in log.replay()] == ["task", "after"]


def test_concurrent_appends_stay_valid(tmp_path):
    log = EventLog(tmp_path)

    def worker():
        for i in range(25):
            log.append(type="task", source="thread", data={"i": i})

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    events = log.replay()
    assert len(events) == 100
    assert {event.type for event in events} == {"task"}


def test_follow_yields_only_new_events(tmp_path):
    log = EventLog(tmp_path)
    log.append(type="old", source="test", data={})
    ready = threading.Event()
    seen: list[str] = []

    def consume():
        for event in log.follow(poll_s=0.01, ready=ready):
            seen.append(event.type)
            break

    thread = threading.Thread(target=consume)
    thread.start()
    assert ready.wait(timeout=2)
    log.append(type="new", source="test", data={})
    thread.join(timeout=2)

    assert seen == ["new"]


def test_ref_null_round_trips(tmp_path):
    log = EventLog(tmp_path)

    log.append(type="task", source="test", ref=None, data={})

    assert log.replay()[0].ref is None


def test_replay_filters_by_day(tmp_path):
    moments = iter(
        [
            datetime(2026, 11, 20, tzinfo=timezone.utc),
            datetime(2026, 11, 21, tzinfo=timezone.utc),
        ]
    )
    log = EventLog(tmp_path, clock=lambda: next(moments))
    log.append(type="a", source="test", data={})
    log.append(type="b", source="test", data={})

    assert [event.type for event in log.replay(day=date(2026, 11, 20))] == ["a"]
    assert [event.type for event in log.replay()] == ["a", "b"]


def test_malformed_complete_line_raises(tmp_path):
    (tmp_path / "2026-11-20.jsonl").write_text('{"id":\n', encoding="utf-8")
    log = EventLog(tmp_path)

    with pytest.raises(EventLogError):
        log.replay()


def test_naive_clock_is_rejected(tmp_path):
    log = EventLog(tmp_path, clock=lambda: datetime(2026, 11, 20, 1, 2, 3))

    with pytest.raises(EventLogError, match="时区"):
        log.append(type="task", source="test", data={})
