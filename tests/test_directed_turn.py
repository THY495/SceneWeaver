import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from support.story import launch

from chatbot.api import create_app
from chatbot.directing import DirectorPlan
from chatbot.director_state import install_plan
from chatbot.npc.context import TurnContext
from chatbot.npc.directed_turn import execute_plan
from chatbot.storage import Conversation, Operation, SceneRecord, Story, Task, now


def test_planned_player_action_does_not_interrupt_after_final_speech_declines_it():
    from chatbot.npc.directed_turn import needs_player_before_next_job

    assert not needs_player_before_next_job(
        {
            "awaiting_player_action": True,
            "wait_for_player": True,
            "expects_reply": False,
            "addressee_id": "player",
        },
        "player",
    )
    assert not needs_player_before_next_job(
        {
            "awaiting_player_action": True,
            "wait_for_player": True,
            "expects_reply": True,
            "addressee_id": "other-npc",
        },
        "player",
    )
    assert needs_player_before_next_job(
        {
            "awaiting_player_action": True,
            "wait_for_player": True,
            "expects_reply": True,
            "addressee_id": "player",
        },
        "player",
    )


@pytest.mark.parametrize(
    "handoff_policy,requires_action,expected_count",
    [
        (None, False, 1),
        ("on_question", False, 1),
        ("continuous", False, 1),
        ("after_beat", False, 3),
        ("after_beat", True, 1),
    ],
)
async def test_early_player_question_reports_incomplete_director_plan(
    db,
    config,
    runtime,
    fake,
    monkeypatch,
    handoff_policy,
    requires_action,
    expected_count,
):
    from chatbot.npc import directed_turn

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "三位先各自说清，我听完再决定。", "early-question")
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
            reason="依次说明",
            jobs=[dict(id=str(i), actor=actor, objective="说明各自所知") for i, actor in enumerate(actors)],
        ),
        handoff_policy=handoff_policy or "on_question",
    )
    if handoff_policy is None:
        ledger.pop("handoff_policy")  # A persisted pre-policy ledger retains old semantics.

    async def authorize(*args, **kwargs):
        return {"objective": args[4].objective}

    async def intention(services, task, turn, actor, **kwargs):
        return {
            "act": "speak",
            "expects_reply": actor.id == actors[0],
            "wait_for_player": True,
            "addressee_id": player,
            "awaiting_player_action": requires_action and actor.id == actors[0],
        }

    async def perform(services, task, turn, actor, intent, step, **kwargs):
        with db.session() as s:
            revision = s.get(Conversation, actor.id).revision
        first = "请先把信递给我，我才能继续。" if requires_action else "你想先听哪部分？"
        return {"answer": first if actor.id == actors[0] else "这是我知道的。", "revision": revision}

    monkeypatch.setattr(directed_turn, "authorize_assignment", authorize)
    monkeypatch.setattr(directed_turn, "plan_actor", intention)
    monkeypatch.setattr(directed_turn, "perform_actor", perform)
    result = await execute_plan(
        SimpleNamespace(db=db, runtime=runtime), task, TurnContext(sid, player, tuple(actors)), ledger, None
    )
    assert result["actor_ids"] == actors[:expected_count]
    assert result["stop_reason"] == "player_turn"
    assert result["interaction_complete"] is (expected_count == 3)
    assert result["director_plan_complete"] is (expected_count == 3)
    assert result["tracked_replies_resolved"] is True
    repeated = await execute_plan(
        SimpleNamespace(db=db, runtime=runtime), task, TurnContext(sid, player, tuple(actors)), ledger, None
    )
    assert [r["event_id"] for r in repeated["segments"]] == [r["event_id"] for r in result["segments"]]
    assert repeated["director_plan_complete"] is result["director_plan_complete"]


async def test_parallel_preparation_ordered_dependency_and_player_handoff(
    db, config, runtime, fake, monkeypatch
):
    from chatbot.npc import directed_turn

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "请讨论", "directed")
    task = runtime.claim("test")
    with db.session() as s:
        seq = s.get(Story, sid).seq
    plan = DirectorPlan(
        beat_id="b",
        handoff=False,
        reason="先并行准备，再回应",
        jobs=[
            dict(id="a", actor=actors[0], objective="解释"),
            dict(id="b", actor=actors[1], objective="补充"),
            dict(id="c", actor=actors[2], objective="回应", depends_on=["a", "b"]),
        ],
    )
    ledger = install_plan(db, task, sid, seq, plan)
    started, both = set(), asyncio.Event()

    async def authorize(*args, **kwargs):
        return {"objective": args[4].objective}

    async def intention(services, task, turn, actor, **kwargs):
        if actor.id == actors[2]:
            assert len(kwargs["replies"]) == 2
        return {
            "act": "speak",
            "purpose": "讨论",
            "wait_for_player": True,
            "addressee_id": player,
            "expects_reply": actor.id == actors[2],
        }

    async def perform(services, task, turn, actor, intent, step, **kwargs):
        assert kwargs["director_assignment"]["objective"] in {"解释", "补充", "回应"}
        if actor.id in actors[:2]:
            started.add(actor.id)
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 2)
        with db.session() as s:
            revision = s.get(Conversation, actor.id).revision
        return {"answer": "这是我的意见。", "revision": revision}

    monkeypatch.setattr(directed_turn, "authorize_assignment", authorize)
    monkeypatch.setattr(directed_turn, "plan_actor", intention)
    monkeypatch.setattr(directed_turn, "perform_actor", perform)
    result = await execute_plan(
        SimpleNamespace(db=db, runtime=runtime), task, TurnContext(sid, player, tuple(actors)), ledger, None
    )
    assert result["actor_ids"] == actors
    assert len({r["event_id"] for r in result["segments"]}) == 3
    assert result["stop_reason"] == "player_turn"
    repeated = await execute_plan(
        SimpleNamespace(db=db, runtime=runtime), task, TurnContext(sid, player, tuple(actors)), ledger, None
    )
    assert repeated["segments"] == result["segments"]
    assert repeated["stop_reason"] == "player_turn"
    # A replacement plan can have new job IDs yet repeat an already public line.
    # It must fail before another event is published; normal replay above stays idempotent.
    with db.session() as s:
        before = s.get(Story, sid).seq
    replacement = install_plan(
        db,
        task,
        sid,
        before,
        DirectorPlan(beat_id="b", reason="恢复", jobs=[dict(id="retry", actor=actors[0], objective="解释")]),
        expected_revision=1,
        recovery_reason="draft_rejected",
    )
    from chatbot.npc.performance import UnsafeDraftExhausted

    with pytest.raises(UnsafeDraftExhausted, match="重复"):
        await execute_plan(
            SimpleNamespace(db=db, runtime=runtime),
            task,
            TurnContext(sid, player, tuple(actors)),
            replacement,
            None,
        )
    with db.session() as s:
        assert s.get(Story, sid).seq == before


async def test_parallel_candidate_is_refreshed_when_earlier_actor_asks_it_a_question(
    db, config, runtime, fake, monkeypatch
):
    from chatbot.event_projection import requests_for
    from chatbot.npc import directed_turn

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "请你们交流", "parallel-question")
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
            reason="两个角色先准备独立意见",
            jobs=[
                dict(id="ask", actor=actors[0], objective="说出看法"),
                dict(id="respond", actor=actors[1], objective="说出看法"),
            ],
        ),
    )
    other_prepared = asyncio.Event()
    second_calls = []

    async def authorize(*args, **kwargs):
        return {"objective": args[4].objective}

    async def intention(services, task, turn, actor, **kwargs):
        if actor.id == actors[0]:
            return {
                "act": "speak",
                "purpose": "询问同伴",
                "addressee_id": actors[1],
                "expects_reply": True,
                "wait_for_player": False,
            }
        with db.session() as s:
            pending = [
                row
                for row in requests_for(s, actor.id)
                if row["task_id"] == task.id and row["target_id"] == actor.id and row["status"] == "open"
            ]
        second_calls.append([row["question_id"] for row in pending])
        return {
            "act": "speak",
            "purpose": "回答同伴" if pending else "独立看法",
            "addressee_id": actors[0] if pending else player,
            "reply_to": pending[0]["question_id"] if pending else None,
            "expects_reply": False,
            "wait_for_player": False,
        }

    async def perform(services, task, turn, actor, intent, step, **kwargs):
        if actor.id == actors[0]:
            await asyncio.wait_for(other_prepared.wait(), 2)
        else:
            other_prepared.set()
        with db.session() as s:
            revision = s.get(Conversation, actor.id).revision
        return {
            "answer": "你怎么看？" if actor.id == actors[0] else "我听到了，但还不确定。",
            "revision": revision,
        }

    monkeypatch.setattr(directed_turn, "authorize_assignment", authorize)
    monkeypatch.setattr(directed_turn, "plan_actor", intention)
    monkeypatch.setattr(directed_turn, "perform_actor", perform)
    result = await execute_plan(
        SimpleNamespace(db=db, runtime=runtime),
        task,
        TurnContext(sid, player, tuple(actors)),
        ledger,
        None,
    )
    assert len(second_calls) == 2
    assert second_calls[0] == []
    assert len(second_calls[1]) == 1
    assert result["actor_ids"] == actors[:2]
    assert result["segments"][1]["intent"]["reply_to"] == second_calls[1][0]
    assert result["pending_reply_ids"] == []
    assert result["interaction_complete"] is True


async def test_autonomous_plan_stops_before_actor_calls_when_player_starts_typing(
    db, config, runtime, fake, monkeypatch
):
    from sqlalchemy import select

    from chatbot.npc import directed_turn

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "请讨论", "foreground-gate")
    task = runtime.claim("test")
    with db.session.begin() as s:
        row = s.get(Story, sid)
        seq = row.seq
        actor = s.get(Conversation, player)
        actor.current_state = {
            **actor.current_state,
            "presence": {"until": now().timestamp() + 60, "visible": True, "typing": True},
        }
        stored_task = s.get(Task, task.id)
        stored_task.payload = {**stored_task.payload, "autonomous": True}
    task.payload = {**task.payload, "autonomous": True}
    ledger = install_plan(
        db,
        task,
        sid,
        seq,
        DirectorPlan(
            beat_id="b", reason="验证前台取消", jobs=[dict(id="a", actor=actors[0], objective="讨论")]
        ),
    )

    async def unexpected(*args, **kwargs):
        raise AssertionError("foreground gate should stop before another model request")

    monkeypatch.setattr(directed_turn, "authorize_assignment", unexpected)
    result = await execute_plan(
        SimpleNamespace(db=db, runtime=runtime), task, TurnContext(sid, player, tuple(actors)), ledger, None
    )
    assert result["stop_reason"] == "paused"
    assert result["segments"] == []
    with db.session() as s:
        gate = s.get(Operation, f"{task.id}:preparation-gate:0:before_assignment")
        assert gate.payload == {"reason": "player_typing", "stage": "before_assignment"}
        assert not list(
            s.scalars(
                select(SceneRecord).where(
                    SceneRecord.task_id == task.id,
                    SceneRecord.actor_id != player,
                )
            )
        )


async def test_autonomous_typing_gate_skips_director_model_call(db, config, runtime, fake, monkeypatch):
    from chatbot.npc import directed_scene

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "请讨论", "before-director")
    task = runtime.claim("test")
    with db.session.begin() as s:
        actor = s.get(Conversation, player)
        actor.current_state = {
            **actor.current_state,
            "presence": {"until": now().timestamp() + 60, "visible": True, "typing": True},
        }
    task.payload = {**task.payload, "autonomous": True}

    async def unexpected(*args, **kwargs):
        raise AssertionError("director should not run after typing begins")

    monkeypatch.setattr(directed_scene, "prepare_scene_plan", unexpected)
    result = await directed_scene.respond_directed(
        SimpleNamespace(db=db), task, TurnContext(sid, player, tuple(actors)), None
    )
    assert result["stop_reason"] == "paused"
    with db.session() as s:
        gate = s.get(Operation, f"{task.id}:preparation-gate:0:before_director")
        assert gate.payload == {"reason": "player_typing", "stage": "before_director"}
