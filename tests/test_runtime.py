from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from chatbot.runtime import StaleTask
from chatbot.storage import Event, Message, Task, now


def test_submit_idempotent_and_transactional_outbox(db, runtime):
    c = runtime.create_session()
    first = runtime.submit(c.id, "local", "你好", "request-1")
    second = runtime.submit(c.id, "local", "你好", "request-1")
    assert first.id == second.id
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Message)) == 1
        assert s.scalar(select(func.count()).select_from(Task)) == 2
        assert s.scalar(select(func.count()).select_from(Event)) == 1


def test_concurrent_duplicate_input_is_single_effect(db, runtime):
    c = runtime.create_session()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: runtime.submit(c.id, "local", "预算5000", "same").id, range(4)))
    assert len(set(ids)) == 1


def test_user_correction_fences_old_output(db, runtime):
    c = runtime.create_session()
    runtime.submit(c.id, "local", "预算3000", "first")
    t = runtime.claim("w1")
    runtime.publish(t.id, t.lease_version, "answer_delta", {"text": "正在比较"})
    new = runtime.submit(c.id, "local", "改成5000", "second")
    with pytest.raises(StaleTask):
        runtime.publish(t.id, t.lease_version, "answer_delta", {"text": "旧预算"})
    assert not runtime.finish(t.id, t.lease_version, {"answer": "错误"})
    assert new.epoch > t.epoch


def test_lease_recovery_fences_previous_worker(db, runtime):
    c = runtime.create_session()
    runtime.submit(c.id, "local", "你好", "one")
    old = runtime.claim("old")
    with db.session.begin() as s:
        s.get(Task, old.id).lease_until = now() - timedelta(seconds=1)
    fresh = runtime.claim("new")
    assert fresh.id == old.id and fresh.lease_version > old.lease_version
    assert not runtime.finish(old.id, old.lease_version, {"answer": "old"})
    assert runtime.finish(fresh.id, fresh.lease_version, {"answer": "new"})
    assert not runtime.finish(fresh.id, fresh.lease_version, {"answer": "duplicate"})
    with db.session() as s:
        assert list(s.scalars(select(Message.content).where(Message.role == "assistant"))) == ["new"]


def test_concurrent_claims_never_share_job(db, runtime):
    for i in range(6):
        c = runtime.create_session()
        runtime.submit(c.id, "local", "hello", str(i))
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = list(pool.map(lambda i: runtime.claim(f"w{i}"), range(6)))
    assert len({t.id for t in jobs}) == 6


def test_cancel_and_expire_have_terminal_events(db, runtime):
    c = runtime.create_session()
    t = runtime.submit(c.id, "local", "hello", "a")
    runtime.cancel(t.id, "local")
    runtime.cancel(t.id, "local")
    t2 = runtime.submit(c.id, "local", "next", "b")
    with db.session.begin() as s:
        s.get(Task, t2.id).deadline = now() - timedelta(seconds=1)
    runtime.expire()
    with db.session() as s:
        assert s.get(Task, t2.id).status == "failed"
        assert list(s.scalars(select(Event.kind).order_by(Event.id))).count("cancelled") == 1


def test_cross_owner_cannot_submit_cancel(db, runtime):
    c = runtime.create_session("other")
    t = runtime.submit(c.id, "other", "secret", "x")
    with pytest.raises(KeyError):
        runtime.submit(c.id, "local", "hi", "x")
    with pytest.raises(KeyError):
        runtime.cancel(t.id, "local")
