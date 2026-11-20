import threading
from datetime import datetime, timedelta, timezone

from audi28.sessions import SessionNotFound, SessionStore


def _clock(start: datetime):
    current = {"now": start}

    def clock() -> datetime:
        current["now"] = current["now"] + timedelta(seconds=1)
        return current["now"]

    return clock


def test_session_survives_restart_and_lists_without_closed(tmp_path):
    clock = _clock(datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc))
    store = SessionStore(tmp_path, clock=clock)
    created = store.create(
        title="跑测试",
        backend="deepseek",
        backend_session_id="fake-1",
        workspace="/tmp/deepseek-workspace",
        task_id="msg-01",
    )
    assert created.session_id.startswith("ses-")
    assert created.pending_questions == 0
    assert created.status == "open"

    restarted = SessionStore(tmp_path, clock=clock)
    again = restarted.get(created.session_id)
    assert again is not None
    assert again.backend_session_id == "fake-1"
    assert again.title == "跑测试"
    assert again.created_ts == created.created_ts

    restarted.close(created.session_id)
    assert restarted.list() == []
    assert restarted.list(include_closed=True)[0].status == "closed"


def test_close_unknown_session_raises(tmp_path):
    store = SessionStore(tmp_path)
    try:
        store.close("ses-missing")
    except SessionNotFound as exc:
        assert "ses-missing" in str(exc)
    else:
        raise AssertionError("expected SessionNotFound")


def test_stale_touch_cannot_reopen_or_drop_task_id(tmp_path):
    for index in range(25):
        store = SessionStore(tmp_path / f"run-{index}")
        created = store.create(
            title="并发",
            backend="deepseek",
            backend_session_id="fake",
            workspace="",
            task_id="msg-old",
        )
        barrier = threading.Barrier(2)

        def close_it() -> None:
            barrier.wait(timeout=2)
            store.close(created.session_id)

        def touch_it() -> None:
            barrier.wait(timeout=2)
            store.touch(created.session_id, task_id="msg-new")

        threads = [threading.Thread(target=close_it), threading.Thread(target=touch_it)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        final = store.get(created.session_id)
        assert final is not None
        assert final.status == "closed"
        assert final.last_task_id in {"msg-old", "msg-new"}


def test_most_recent_continuable_skips_closed(tmp_path):
    clock = _clock(datetime(2026, 11, 20, 1, 0, tzinfo=timezone.utc))
    store = SessionStore(tmp_path, clock=clock)
    older = store.create(title="旧的", backend="deepseek", backend_session_id="a", workspace="", task_id=None)
    newer = store.create(title="新的", backend="deepseek", backend_session_id="b", workspace="", task_id=None)
    store.touch(older.session_id, task_id="msg-9")
    assert store.most_recent_continuable().session_id == older.session_id
    store.close(older.session_id)
    assert store.most_recent_continuable().session_id == newer.session_id
