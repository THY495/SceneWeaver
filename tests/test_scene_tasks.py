import json

import pytest
from sqlalchemy import select
from support.agreements import command
from support.scene import world

from chatbot.agents import Agents
from chatbot.observations import visible_records
from chatbot.runtime import StaleTask
from chatbot.scene_state import objects
from chatbot.scene_tasks import schedule_scenes
from chatbot.storage import ActionAttempt, Conversation, Message, Story, Task, now


async def test_offscreen_execution_has_no_player_session_or_delivery(db, config, runtime, fake):
    story, actors, player, data = world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    assert tid
    task = runtime.claim("scene", background=True)
    assert task.id == tid and task.session_id is None and task.kind == "scene_run"
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
    # A message in another scene does not supersede this task or enter NPC context.
    runtime.submit(player, config.workspace_owner, "场外玩家秘密紫色蚂蚁", "unrelated-input")
    runtime.check(task.id, task.lease_version)
    with db.session() as s:
        player_revision = s.get(Conversation, player).revision
    result = await Agents(db, config, runtime, fake).execute(task)
    assert result["answer"] == "" and result["segments"] == []
    assert runtime.finish(task.id, task.lease_version, result)
    assert "紫色蚂蚁" not in json.dumps(fake.calls, ensure_ascii=False, default=str)
    with db.session() as s:
        assert objects(s, story)[0]["holder_id"] == actors[1]
        assert objects(s, story, player)[0]["holder_id"] == actors[0]
        assert s.get(Conversation, player).revision == player_revision
        assert not s.scalar(select(Message).where(Message.task_id == tid))
        assert s.get(ActionAttempt, f"task:{tid}:0").status == "completed"
        assert any(event.task_id == tid for event in visible_records(s, actors[1]))
        assert not any(event.task_id == tid for event in visible_records(s, player))


def test_opportunity_consumed_once_and_pause_does_not_catch_up(db, config, runtime, fake):
    story, _, _, _ = world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    with db.session.begin() as s:
        s.get(Task, tid).status = "completed"
        st = s.get(Story, story)
        st.settings = {**st.settings, "scene_last_issued": {}}
    assert schedule_scenes(db, runtime, config.workspace_owner) is None
    with db.session.begin() as s:
        st = s.get(Story, story)
        st.settings = {**st.settings, "activity_until": 0}
    assert schedule_scenes(db, runtime, config.workspace_owner) is None
    with db.session.begin() as s:
        st = s.get(Story, story)
        st.settings = {**st.settings, "activity_until": now().timestamp() + 100}
    assert schedule_scenes(db, runtime, config.workspace_owner) is None


@pytest.mark.parametrize("change", ["paused", "offline", "player_arrives", "npc_leaves"])
def test_stale_scene_stops_before_commit(db, config, runtime, fake, change):
    story, actors, player, _ = world(db, config, runtime, fake)
    schedule_scenes(db, runtime, config.workspace_owner)
    task = runtime.claim("scene", background=True)
    with db.session.begin() as s:
        st = s.get(Story, story)
        if change == "paused":
            st.settings = {**st.settings, "paused": True}
        elif change == "offline":
            st.settings = {**st.settings, "activity_until": 0}
        else:
            actor = s.get(Conversation, player if change == "player_arrives" else actors[1])
            actor.current_state = {
                **actor.current_state,
                "location": "main" if change == "player_arrives" else "away",
            }
        revision = s.get(Conversation, actors[0]).revision
    with pytest.raises(StaleTask):
        runtime.commit_role_segment(
            task, actors[0], "不能提交", step=0, revision=revision, intent={"act": "speak"}
        )


def test_global_background_scene_capacity_is_one(db, config, runtime, fake):
    _, _, _, _ = world(db, config, runtime, fake)
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    with db.session.begin() as s:
        task = s.get(Task, tid)
        runtime.enqueue(s, "scene_run", config.workspace_owner, task.payload, priority=70)
    assert runtime.claim("scene-a", background=True).kind == "scene_run"
    assert runtime.claim("scene-b", background=True) is None


async def test_scene_dialogue_uses_review_and_replays_committed_receipt(db, config, runtime, fake):
    story, actors, player, _ = world(db, config, runtime, fake)
    schedule_scenes(db, runtime, config.workspace_owner)
    task = runtime.claim("scene-dialogue", background=True)
    agents = Agents(db, config, runtime, fake)
    result = await agents.execute(task)
    assert result["route"] == "scene" and result["answer"] == ""
    assert {call[0] for call in fake.calls} >= {"role_intent", "dialogue", "actor_review"}
    count = len(fake.calls)
    await agents.execute(task)
    assert len(fake.calls) == count
    with db.session() as s:
        assert len([e for e in visible_records(s, actors[0]) if e.task_id == task.id]) == 1
        assert not [e for e in visible_records(s, player) if e.task_id == task.id]
        before = objects(s, story, player)[0]
    assert before["holder_id"] == actors[0]


@pytest.mark.parametrize("goal_only", [False, True])
def test_foreground_claim_priority_and_new_incoming_opportunity(
    db, config, runtime, fake, monkeypatch, goal_only
):
    from chatbot.observations import append_record

    story, actors, player, _ = world(db, config, runtime, fake)
    if goal_only:
        monkeypatch.setattr("chatbot.agreements.proposals", lambda *args: [])
    tid = schedule_scenes(db, runtime, config.workspace_owner)
    foreground = runtime.submit(player, config.workspace_owner, "请稍等", "foreground")
    background = runtime.claim("defer-scene", background=True)
    assert background is None or background.kind != "scene_run"
    with db.session.begin() as s:
        s.get(Task, foreground.id).status = "completed"
        s.get(Task, tid).status = "completed"
        st = s.get(Story, story)
        st.settings = {**st.settings, "scene_last_issued": {}}
        a = s.get(Conversation, actors[0])
        append_record(
            s,
            story_id=story,
            actor=a,
            kind="speech",
            text="我先想一想。",
            observer_ids=[a.id],
            dedupe="self-only",
        )
    assert schedule_scenes(db, runtime, config.workspace_owner) is None
    with db.session.begin() as s:
        b = s.get(Conversation, actors[1])
        append_record(
            s,
            story_id=story,
            actor=b,
            kind="speech",
            text="我已经准备好。",
            observer_ids=actors[:2],
            dedupe="incoming",
        )
    assert bool(schedule_scenes(db, runtime, config.workspace_owner)) is goal_only
