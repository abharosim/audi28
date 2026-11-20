from audi28.router import RouteContext, RulesRouter, SessionSnapshot, TaskMessage


def _router() -> RulesRouter:
    return RulesRouter(
        default="deepseek",
        new_task_phrases=("新任务", "开一个新的", "另外一个任务", "new task"),
        not_new_task_phrases=("不是新任务", "接着上一个"),
        self_phrases=("你直接回答", "不用 DeepSeek"),
    )


def _message(text: str, *, session_id: str | None = None, target: str | None = None) -> TaskMessage:
    return TaskMessage(
        message_id="msg-01",
        kind="task",
        text=text,
        session_id=session_id,
        target=target,
    )


def _open(session_id: str, updated_ts: str, status: str = "open") -> SessionSnapshot:
    return SessionSnapshot(session_id=session_id, status=status, updated_ts=updated_ts)


def test_new_task_phrases_open_a_new_session_even_when_one_is_open():
    context = RouteContext(sessions=(_open("ses-old", "2026-11-20T00:00:00+00:00"),))
    for text in ("新任务：跑测试", "请开一个新的", "另外一个任务", "start a New Task"):
        decision = _router().decide(_message(text), context)
        assert decision.target == "deepseek"
        assert decision.session_action == "new"
        assert decision.session_id is None
        assert decision.reason


def test_negations_do_not_open_a_new_session():
    context = RouteContext(sessions=(_open("ses-old", "2026-11-20T00:00:00+00:00"),))
    for text in ("这不是新任务，接着改", "接着上一个"):
        decision = _router().decide(_message(text), context)
        assert decision.session_action == "continue", text
        assert decision.session_id == "ses-old"
        assert "命中" not in decision.reason


def test_follow_up_continues_the_most_recent_open_session():
    context = RouteContext(
        sessions=(
            _open("ses-older", "2026-11-20T00:00:00+00:00"),
            _open("ses-newer", "2026-11-20T02:00:00+00:00", status="idle"),
            _open("ses-closed", "2026-11-20T03:00:00+00:00", status="closed"),
        )
    )
    decision = _router().decide(_message("把失败的补上"), context)
    assert decision.session_action == "continue"
    assert decision.session_id == "ses-newer"
    assert decision.rule_id == "continue-latest"


def test_no_open_session_opens_a_new_one():
    decision = _router().decide(_message("接着上一个"), RouteContext(sessions=()))
    assert decision.session_action == "new"
    assert "没有仍开放" in decision.reason


def test_explicit_session_id_overrides_a_new_task_phrase():
    context = RouteContext(sessions=(_open("ses-latest", "2026-11-20T04:00:00+00:00"),))
    decision = _router().decide(_message("新任务：全部重写", session_id="ses-named"), context)
    assert decision.session_action == "continue"
    assert decision.session_id == "ses-named"
    assert decision.rule_id == "explicit-session"


def test_explicit_targets():
    router = _router()
    deepseek = router.decide(_message("随便", target="deepseek"), RouteContext(sessions=()))
    assert deepseek.target == "deepseek"
    audi = router.decide(_message("新任务", target="audi_self"), RouteContext(sessions=()))
    assert audi.target == "audi_self"
    assert audi.session_action == "none"
    legacy = router.decide(_message("随便", target="self"), RouteContext(sessions=()))
    assert legacy.target == "audi_self"


def test_self_phrase_is_not_handed_to_deepseek():
    decision = _router().decide(_message("你直接回答就行"), RouteContext(sessions=()))
    assert decision.target == "audi_self"
    assert decision.session_action == "none"
    assert decision.reason
