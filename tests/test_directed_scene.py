from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from support.story import launch

from chatbot.api import create_app
from chatbot.directing import Beat, DirectorPlan, SceneDirection
from chatbot.director_state import install_plan
from chatbot.npc.context import TurnContext
from chatbot.npc.directed_scene import respond_directed
from chatbot.npc.performance import UnsafeDraftExhausted
from chatbot.storage import Conversation, SceneRecord, Story, Task


def test_recovery_preserves_scheduled_eligible_speakers_only():
    from chatbot.npc.director import pending_recovery_actors

    ledger = {
        "plan": {"jobs": [
            {"id": "first", "actor": "a"},
            {"id": "second", "actor": "b"},
            {"id": "third", "actor": "c"},
        ]},
        "candidates": {"first": {"status": "committed", "event_id": "e"}},
    }
    assert pending_recovery_actors(ledger, {"a", "b", "c"}, 2) == ["b", "c"]
    assert pending_recovery_actors(ledger, {"a", "b"}, 2) == ["b"]
    assert pending_recovery_actors(ledger, {"a", "b", "c"}, 1) == ["b"]


@pytest.mark.parametrize("kind", ["rejected", "budget", "cancelled", "second_failure"])
async def test_replanning_is_bounded_and_never_retries_budget_or_cancel(db, monkeypatch, kind):
    from chatbot.npc import directed_scene
    from chatbot.npc.assignment import AssignmentRejected
    from chatbot.providers import BudgetExceeded
    from chatbot.runtime import StaleTask

    task = SimpleNamespace(id="t", lease_version=1)
    scope = SimpleNamespace(
        node_id="n",
        entry_event_id="e",
        beat_id="b",
        direction=SimpleNamespace(beats=[SimpleNamespace(id="b", max_segments=3)]),
    )
    check = Mock(side_effect=StaleTask("cancelled") if kind == "cancelled" else None)
    failure = BudgetExceeded("budget") if kind == "budget" else AssignmentRejected("rejected")
    execute = AsyncMock(side_effect=[failure, {"stop_reason": "player_turn"}])
    plan = AsyncMock(return_value={"recovery_count": 1})
    monkeypatch.setattr(directed_scene, "execute_plan", execute)
    monkeypatch.setattr(directed_scene, "prepare_scene_plan", plan)
    monkeypatch.setattr(directed_scene, "snapshot", lambda *args: scope)
    ledger = {"recovery_count": 1 if kind == "second_failure" else 0}
    services = SimpleNamespace(db=db, runtime=SimpleNamespace(check=check))
    if kind == "rejected":
        updated, result = await directed_scene.execute_recoverable(services, task, None, ledger, None, scope)
        assert updated["recovery_count"] == 1 and result["stop_reason"] == "player_turn"
        assert plan.call_args.kwargs["recovery_reason"] == "assignment_rejected"
        assert execute.await_count == 2
    else:
        with pytest.raises((BudgetExceeded, StaleTask, AssignmentRejected)):
            await directed_scene.execute_recoverable(services, task, None, ledger, None, scope)
        plan.assert_not_called()


@pytest.mark.parametrize("stop,expected", [("limit", 2), ("player_turn", 1)])
async def test_continuous_beats_stop_at_player_boundary(db, monkeypatch, stop, expected):
    from chatbot import auto_routing
    from chatbot.npc import directed_scene

    direction = SceneDirection(
        objective="讨论",
        beats=[
            Beat(
                id=key,
                title=key,
                objective="讨论",
                completion="说完",
                participants=["a"],
                handoff="continuous",
            )
            for key in ["first", "second"]
        ],
    )
    state = {"index": 0}

    def snapshot(*args, beat_id=None):
        return SimpleNamespace(
            direction=direction,
            world_seq=12,
            node_id="node",
            entry_event_id="entry",
            beat_id=beat_id or direction.beats[min(state["index"], 1)].id,
            finished=state["index"] >= 2,
        )

    async def assess(*args):
        state["index"] += 1
        return {"advanced": True}

    async def plan(*args, **kwargs):
        return {"plan": {"beat_id": "second"}}

    execute = AsyncMock(
        side_effect=lambda *args: {"stop_reason": stop, "segments": [], "pending_reply_ids": []}
    )
    prepare = AsyncMock(side_effect=plan)
    monkeypatch.setattr(directed_scene, "snapshot", snapshot)
    monkeypatch.setattr(directed_scene, "assess_beat", assess)
    monkeypatch.setattr(directed_scene, "prepare_scene_plan", prepare)
    monkeypatch.setattr(directed_scene, "execute_plan", execute)
    monkeypatch.setattr(auto_routing, "assess", AsyncMock())
    result = await respond_directed(
        SimpleNamespace(db=db, provider=None),
        SimpleNamespace(id="t", kind="respond", epoch=3),
        SimpleNamespace(player_id="player"),
        None,
        {"plan": {"beat_id": "first"}},
    )
    assert execute.await_count == expected
    assert prepare.await_count == expected - 1
    if stop == "player_turn":
        assert result["director_handoff"] == {
            "world_seq": 12, "node_id": "node", "entry_event_id": "entry", "epoch": 3
        }
    else:
        assert "director_handoff" not in result


@pytest.mark.parametrize("failure_kind", ["draft", "assignment"])
async def test_rejected_later_directed_segment_keeps_committed_speech_without_routing(
    db, config, runtime, fake, monkeypatch, failure_kind
):
    from sqlalchemy import select

    from chatbot import auto_routing
    from chatbot.npc import directed_scene, directed_turn
    from chatbot.npc.assignment import AssignmentRejected

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "请先商量", "partial-safety")
    task = runtime.claim("test")
    with db.session() as s:
        seq = s.get(Story, sid).seq
    ledger = install_plan(
        db,
        task,
        sid,
        seq,
        DirectorPlan(
            beat_id="b",
            handoff=False,
            reason="先发言再接续",
            jobs=[
                dict(id="first", actor=actors[0], objective="先发言"),
                dict(id="second", actor=actors[1], objective="接续", depends_on=["first"]),
            ],
        ),
    )
    ledger["recovery_count"] = 1
    monkeypatch.setattr(
        directed_scene,
        "snapshot",
        lambda *args, **kwargs: SimpleNamespace(node_id="n", entry_event_id="e", beat_id="b"),
    )

    async def authorize(*args, **kwargs):
        if failure_kind == "assignment" and args[4].id == "second":
            raise AssignmentRejected("后续任务越过角色知情边界")
        return {"objective": args[4].objective}

    async def intention(*args, **kwargs):
        return {"act": "speak", "purpose": "讨论", "wait_for_player": False}

    async def perform(services, task, turn, actor, intent, step, **kwargs):
        if step and failure_kind == "draft":
            raise UnsafeDraftExhausted("后续台词无依据")
        with db.session() as s:
            revision = s.get(Conversation, actor.id).revision
        return {"answer": "我先说说自己的看法。", "revision": revision}

    monkeypatch.setattr(directed_turn, "authorize_assignment", authorize)
    monkeypatch.setattr(directed_turn, "plan_actor", intention)
    monkeypatch.setattr(directed_turn, "perform_actor", perform)
    monkeypatch.setattr(auto_routing, "assess", AsyncMock(side_effect=AssertionError("must not route")))
    result = await respond_directed(
        SimpleNamespace(db=db, runtime=runtime, provider=None),
        task,
        TurnContext(sid, player, tuple(actors)),
        None,
        ledger,
    )
    assert result["stop_reason"] == "safety_rejected"
    assert result["interaction_complete"] is False
    assert [segment["text"] for segment in result["segments"]] == ["我先说说自己的看法。"]
    assert runtime.finish(task.id, task.lease_version, result=result)
    with db.session() as s:
        assert s.get(Task, task.id).status == "completed"
        assert len(list(s.scalars(select(SceneRecord).where(SceneRecord.task_id == task.id)))) == 2


async def test_rejected_first_directed_segment_still_fails(db, monkeypatch):
    from chatbot.npc import directed_scene

    monkeypatch.setattr(
        directed_scene,
        "snapshot",
        lambda *args, **kwargs: SimpleNamespace(node_id="n", entry_event_id="e", beat_id="b"),
    )
    monkeypatch.setattr(
        directed_scene,
        "execute_recoverable",
        AsyncMock(side_effect=UnsafeDraftExhausted("首段无依据")),
    )
    with pytest.raises(UnsafeDraftExhausted, match="首段"):
        await respond_directed(
            SimpleNamespace(db=db),
            SimpleNamespace(id="uncommitted", kind="respond"),
            SimpleNamespace(player_id="player"),
            None,
            {"plan": {"beat_id": "b"}},
        )
