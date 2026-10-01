from datetime import timedelta

import pytest
from sqlalchemy import func, select
from support.agreements import command, consent, setup

from chatbot.action_attempts import pause_task, reserve
from chatbot.agreements import proposals
from chatbot.observations import append_record
from chatbot.providers import ProviderError
from chatbot.runtime import StaleTask
from chatbot.storage import ActionAttempt, Conversation, SceneRecord, Task, now


def ready(db, config, runtime, fake):
    story, actors, player, _, data = setup(db, config, fake)
    with db.session.begin() as s:
        for aid in actors[:2]:
            consent(s, s.get(Conversation, aid), data)
    runtime.submit(player, config.workspace_owner, "馆长，请按提案行动。", "act")
    task = runtime.claim("attempt-test")
    return story, actors, player, data, task


def commit(db, runtime, task, actor_id, cmd):
    with db.session() as s:
        revision = s.get(Conversation, actor_id).revision
    return runtime.commit_role_segment(
        task,
        actor_id,
        "preview",
        step=0,
        revision=revision,
        intent={"act": "object_action", "object_action": cmd},
    )


def test_attempt_success_is_atomic_and_retry_never_repeats(db, config, runtime, fake):
    story, actors, _, data, task = ready(db, config, runtime, fake)
    cmd = command(data)
    with pytest.raises(StaleTask, match="租约"):
        commit(db, runtime, task, actors[0], cmd)
    key = reserve(db, task, actors[0], 0, cmd)
    eid = commit(db, runtime, task, actors[0], cmd)
    assert commit(db, runtime, task, actors[0], cmd) == eid
    with db.session() as s:
        attempt = s.get(ActionAttempt, key)
        assert attempt.status == "completed" and attempt.result_event_id == eid
        assert proposals(s, story, actors[0])[0]["completed"][data["items"][0]["id"]] == eid
        assert s.scalar(select(func.count()).select_from(SceneRecord).where(SceneRecord.dedupe == key)) == 1


def test_expired_lease_is_fenced_and_reclaim_can_resume(db, config, runtime, fake):
    _, actors, _, data, task = ready(db, config, runtime, fake)
    cmd = command(data)
    reserve(db, task, actors[0], 0, cmd)
    with db.session.begin() as s:
        current = s.get(Task, task.id)
        current.lease_version += 1
        fresh = current
    with pytest.raises(StaleTask):
        commit(db, runtime, task, actors[0], cmd)
    reserve(db, fresh, actors[0], 0, cmd)
    pause_task(db, task, "interrupted")  # Late old worker cannot pause the new lease.
    with db.session() as s:
        assert s.get(ActionAttempt, f"task:{task.id}:0").status == "reserved"
    assert commit(db, runtime, fresh, actors[0], cmd)


def test_technical_pause_has_no_narrative_event_and_backoff_is_bounded(db, config, runtime, fake):
    story, actors, _, data, task = ready(db, config, runtime, fake)
    cmd = command(data)
    key = reserve(db, task, actors[0], 0, cmd)
    with db.session() as s:
        before = s.scalar(select(func.count()).select_from(SceneRecord))
    pause_task(db, task, "provider_error")
    with db.session() as s:
        attempt = s.get(ActionAttempt, key)
        assert attempt.status == "paused" and attempt.pause_reason == "provider_error"
        assert 0 < (attempt.retry_at - now()).total_seconds() <= 60
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == before
        assert proposals(s, story, actors[0])[0]["failures"] == {}
    with pytest.raises(ProviderError, match="退避"):
        reserve(db, task, actors[0], 0, cmd)
    with db.session.begin() as s:
        s.get(ActionAttempt, key).retry_at = now() - timedelta(seconds=1)
    reserve(db, task, actors[0], 0, cmd)
    assert commit(db, runtime, task, actors[0], cmd)


def test_world_failure_is_private_committed_and_not_a_technical_outage(db, config, runtime, fake):
    story, actors, player, data, task = ready(db, config, runtime, fake)
    cmd = command(data)
    key = reserve(db, task, actors[0], 0, cmd)
    with db.session.begin() as s:
        b = s.get(Conversation, actors[1])
        append_record(
            s,
            story_id=story,
            actor=b,
            kind="object_state",
            text="隐藏变化",
            observer_ids=[b.id],
            dedupe="hidden-change",
            payload={
                "object": {"id": "box", "name": "铜盒", "version": 2, "holder_id": b.id, "location": "secret"}
            },
        )
    eid = commit(db, runtime, task, actors[0], cmd)
    with db.session() as s:
        attempt = s.get(ActionAttempt, key)
        assert attempt.status == "failed" and attempt.pause_reason is None
        event = s.get(SceneRecord, eid)
        assert event.kind == "action" and "secret" not in event.text
        assert proposals(s, story, actors[0])[0]["item_states"][data["items"][0]["id"]]["state"] == "failed"
        assert proposals(s, story, actors[1])[0]["failures"] == {}
        assert proposals(s, story, player) == []
    assert commit(db, runtime, task, actors[0], cmd) == eid


def test_only_one_live_task_can_own_an_item(db, config, runtime, fake):
    _, actors, player, data, first = ready(db, config, runtime, fake)
    cmd = command(data)
    key = reserve(db, first, actors[0], 0, cmd)
    with db.session.begin() as s:
        runtime.enqueue(
            s,
            "respond",
            config.workspace_owner,
            {"text": "继续", "audience": [player, *actors]},
            session_id=player,
            epoch=first.epoch,
        )
    second = runtime.claim("second-worker")
    with pytest.raises(ProviderError, match="有效执行"):
        reserve(db, second, actors[0], 0, cmd)
    with db.session.begin() as s:
        s.get(Task, first.id).lease_until = now() - timedelta(seconds=1)
    reserve(db, second, actors[0], 0, cmd)
    with db.session() as s:
        assert s.get(ActionAttempt, key).pause_reason == "lease_expired"
    with pytest.raises(StaleTask):
        commit(db, runtime, first, actors[0], cmd)
    assert commit(db, runtime, second, actors[0], cmd)


async def test_actual_agent_path_reserves_then_commits(db, config, runtime, fake):
    from chatbot.agents import Agents

    _, actors, _, data, task = ready(db, config, runtime, fake)
    fake.replies["role_intent"] = {
        "act": "object_action",
        "purpose": "履行协议",
        "source_ids": [],
        "deception": False,
        "addressee_id": None,
        "wait_for_player": True,
        "retrieval": "context",
        "object_action": command(data),
    }
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["segments"]
    with db.session() as s:
        attempt = s.get(ActionAttempt, f"task:{task.id}:0")
        assert attempt.status == "completed" and attempt.actor_id == actors[0]


async def test_worker_budget_error_only_pauses_attempt(db, config, runtime, fake):
    from chatbot.providers import BudgetExceeded
    from chatbot.worker import Worker

    _, actors, _, data, task = ready(db, config, runtime, fake)
    worker = Worker(db, config, fake)

    async def execute(current):
        reserve(db, current, actors[0], 0, command(data))
        raise BudgetExceeded("本次测试预算耗尽")

    worker.agents.execute = execute
    with db.session() as s:
        before = s.scalar(select(func.count()).select_from(SceneRecord))
    await worker.run_task(task)
    with db.session() as s:
        attempt = s.get(ActionAttempt, f"task:{task.id}:0")
        assert attempt.status == "paused" and attempt.pause_reason == "budget"
        assert attempt.result_event_id is None
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == before
