from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.scene import world

from chatbot.api import create_app
from chatbot.providers import (
    BudgetExceeded,
    DailyBudgetExceeded,
    ProviderError,
    TransientProviderError,
    http_failure,
)
from chatbot.scene_recovery import park_scene
from chatbot.scene_tasks import schedule_scenes
from chatbot.storage import Conversation, Operation, RoleMemory, Story, Task, now
from chatbot.worker import Worker


async def test_recovery_does_not_repeat_committed_transfer(db, config, runtime, fake):
    from support.agreements import command

    from chatbot.agents import Agents
    from chatbot.scene_state import objects
    from chatbot.storage import SceneRecord

    story, actors, _, data = world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    task = runtime.claim("scene", background=True)
    fake.replies["role_intent"] = {
        "act": "object_action",
        "purpose": "履行约定",
        "source_ids": [],
        "deception": False,
        "addressee_id": None,
        "wait_for_player": True,
        "retrieval": "context",
        "object_action": command(data),
    }
    agents = Agents(db, config, runtime, fake)
    await agents.execute(task)
    count = len(fake.calls)
    assert park_scene(db, task, TransientProviderError("failure after commit"))
    due(db, tid)
    assert schedule_scenes(db, runtime, config.workspace_owner) == tid
    resumed = runtime.claim("scene", background=True)
    await agents.execute(resumed)
    assert len(fake.calls) == count
    with db.session() as s:
        assert objects(s, story)[0]["holder_id"] == actors[1]
        assert len(list(s.scalars(select(SceneRecord).where(SceneRecord.task_id == tid)))) == 1


def due(db, tid):
    with db.session.begin() as s:
        t = s.get(Task, tid)
        t.payload = {**t.payload, "recovery": {**t.payload["recovery"], "not_before": 0}}


async def test_worker_recovery_preserves_receipts_and_call_budget(db, config, runtime, fake):
    story, _, _, _ = world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    task = runtime.claim("scene", background=True)
    with db.session.begin() as s:
        s.get(Task, tid).calls = 2
        s.add(Operation(id="cached", task_id=tid, payload={"value": "stale plan"}))
        s.add(Operation(id="receipt", task_id=tid, payload={"event_id": "committed"}))
    worker = Worker(db, config, fake)

    async def fail(_):
        raise TransientProviderError("temporary transport failure")

    worker.agents.execute = fail
    await worker.run_task(task)
    with db.session.begin() as s:
        t = s.get(Task, tid)
        assert t.status == "waiting"
        assert 20 < t.payload["recovery"]["not_before"] - now().timestamp() <= 30
        t.deadline = now() - timedelta(seconds=1)
        st = s.get(Story, story)
        st.settings = {**st.settings, "activity_until": 0}
    runtime.expire()
    due(db, tid)
    assert schedule_scenes(db, runtime, config.workspace_owner) is None
    with db.session.begin() as s:
        assert s.get(Task, tid).status == "waiting"
        st = s.get(Story, story)
        st.settings = {**st.settings, "activity_until": now().timestamp() + 300}
    assert schedule_scenes(db, runtime, config.workspace_owner) == tid
    with db.session() as s:
        assert s.get(Operation, "cached") is None
        assert s.get(Operation, "receipt") is not None
        assert s.get(Task, tid).calls == 2
    second = runtime.claim("scene", background=True)
    assert second.attempt == 2 and second.lease_version > task.lease_version
    assert not park_scene(db, task, TransientProviderError("stale lease"))
    assert park_scene(db, second, TransientProviderError("again"))
    due(db, tid)
    assert schedule_scenes(db, runtime, config.workspace_owner) == tid
    third = runtime.claim("scene", background=True)
    await worker.run_task(third)
    with db.session() as s:
        assert s.get(Task, tid).status == "failed"
        assert s.get(Task, tid).attempt == 3


@pytest.mark.parametrize(
    "error,retry",
    [
        (BudgetExceeded("task cap"), False),
        (ProviderError("invalid output"), False),
        (http_failure(401), False),
        (http_failure(429), True),
        (http_failure(503), True),
        (DailyBudgetExceeded("daily cap"), True),
    ],
)
def test_failure_classification(db, config, runtime, fake, error, retry):
    world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    task = runtime.claim("scene", background=True)
    assert park_scene(db, task, error) is retry
    if isinstance(error, DailyBudgetExceeded):
        with db.session() as s:
            stamp = now()
            expected = stamp.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            assert s.get(Task, tid).payload["recovery"]["not_before"] == expected.timestamp()


def test_scene_change_cancels_waiting_work(db, config, runtime, fake):
    _, _, player, _ = world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    task = runtime.claim("scene", background=True)
    park_scene(db, task, TransientProviderError("retry"))
    due(db, tid)
    with db.session.begin() as s:
        p = s.get(Conversation, player)
        p.current_state = {**p.current_state, "location": "main"}
    assert schedule_scenes(db, runtime, config.workspace_owner) is None
    with db.session() as s:
        assert s.get(Task, tid).status == "cancelled"


def test_agreement_without_goal_and_creator_cancellation(db, config, runtime, fake):
    story, _, _, _ = world(db, config, runtime, fake)
    with db.session.begin() as s:
        for memory in s.scalars(select(RoleMemory).where(RoleMemory.kind == "goal")):
            s.delete(memory)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    with db.session() as s:
        assert s.get(Task, tid).payload["trigger"]["kind"] == "agreement_ready"
    task = runtime.claim("scene", background=True)
    park_scene(db, task, TransientProviderError("retry"))
    with TestClient(create_app(config, db, fake)) as client:
        path = f"/api/role/stories/{story}/activities"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()[0]["status"] == "waiting"
        assert client.post(f"{path}/{tid}/cancel").status_code == 200
    assert schedule_scenes(db, runtime, config.workspace_owner) is None
    with db.session() as s:
        assert s.get(Task, tid).status == "cancelled"


def test_reply_opportunity_uses_only_participants_observations(db, config, runtime, fake):
    from support.agreements import setup

    from chatbot.observations import visible_records
    from chatbot.scene_tasks import opportunities

    _, actors, _, _, _ = setup(db, config, fake)
    with db.session() as s:
        for aid in actors[:2]:
            actor = s.get(Conversation, aid)
            assert any(
                x["kind"] == "agreement_reply" for x in opportunities(s, actor, visible_records(s, aid))
            )
        outsider = s.get(Conversation, actors[2])
        assert not opportunities(s, outsider, visible_records(s, outsider.id))


def test_withdrawn_trigger_is_not_resumed(db, config, runtime, fake):
    from support.agreements import consent

    _, actors, _, data = world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    task = runtime.claim("scene", background=True)
    assert park_scene(db, task, TransientProviderError("retry"))
    due(db, tid)
    with db.session.begin() as s:
        consent(s, s.get(Conversation, actors[1]), data, decision="withdraw", request="withdraw")
    schedule_scenes(db, runtime, config.workspace_owner)
    with db.session() as s:
        assert s.get(Task, tid).status == "cancelled"
