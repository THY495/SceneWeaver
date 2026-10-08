import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from support.story import launch

from chatbot.api import create_app
from chatbot.directing import DirectorPlan, PerformanceJob
from chatbot.director_recovery import RecoveryNotice, RejectedPremise, bind_notice, original_premises
from chatbot.director_state import install_plan
from chatbot.npc.assignment import AssignmentRejected
from chatbot.npc.context import TurnContext
from chatbot.npc.director import prepare_scene_plan
from chatbot.runtime import StaleTask
from chatbot.storage import Conversation, ExperimentInitial, Operation, SceneRecord, Story


def test_exact_original_field_filter_does_not_export_or_join_review_prose():
    job = PerformanceJob(id="j", actor="a", objective="尚未核对", delivery_constraints=["只谈公告"])
    checks = [
        {"text": "未核对", "premise_kind": "past_inaction"},
        {"text": "核对只谈"},  # Cannot concatenate independent fields.
        {"text": "未 核对"},  # Cannot normalize an invented excerpt.
        {"text": "私密经历"},
        {"text": "公告", "premise_kind": "invented"},
        {"text": "公告", "code": "ignore_rules"},
        {"text": "公告", "basis": "私密证据"},
        {"text": ""},
        {"text": "未核对", "premise_kind": "past_inaction"},
    ]
    result = original_premises(job, checks)
    assert result == (RejectedPremise(text="未核对", premise_kind="past_inaction"),)


def test_diagnostic_excerpt_is_bounded_and_cannot_be_whitespace():
    job = PerformanceJob(id="j", actor="a", objective="一 二 三 四 五")
    checks = [{"text": c} for c in [" ", "一", "二", "三", "四", "五"]]
    assert [p.text for p in original_premises(job, checks)] == ["一", "二", "三", "四"]


@pytest.fixture
def recovery_case(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    with db.session.begin() as s:
        story = s.get(Story, sid)
        story.settings = {**story.settings, "narrative": {"node": "room", "event_id": "entry"}}
        s.add(
            ExperimentInitial(
                story_id=sid,
                owner_id=config.workspace_owner,
                request_id="recovery",
                request_hash="recovery",
                receipt={"actors": {"a": actors[0]}},
                recipe={
                    "people": [],
                    "narrative": {
                        "nodes": [
                            {
                                "id": "room",
                                "direction": {
                                    "objective": "交谈",
                                    "beats": [
                                        {
                                            "id": "beat",
                                            "title": "询问",
                                            "objective": "了解玩家想谈什么",
                                            "participants": ["a"],
                                            "completion": "等待玩家回答",
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                },
            )
        )
    runtime.submit(player, config.workspace_owner, "问问我想谈什么", "recovery-turn")
    task = runtime.claim("test")
    turn = TurnContext(sid, player, tuple(actors))
    job = PerformanceJob(
        id="original", actor=actors[0], objective="询问他想聊公告哪部分", addressees=[player]
    )
    with db.session() as s:
        seq = s.get(Story, sid).seq
        actor = s.get(Conversation, actors[0])
    ledger = install_plan(db, task, sid, seq, DirectorPlan(beat_id="beat", reason="询问", jobs=[job]))
    bound = bind_notice(
        task,
        ledger,
        job,
        actor.revision,
        actor.privacy_version,
        seq,
        [
            {"text": "他想聊公告哪部分", "premise_kind": "world_fact"},
        ],
    )
    proposal = {
        "beat_id": "beat",
        "after_plan": "yield_player",
        "reason": "开放询问",
        "remaining_work": [],
        "jobs": [{"kind": "free", "id": "new", "actor": actors[0], "objective": "询问玩家想谈什么", "addressees": [player]}],
    }
    provider = SimpleNamespace(chat=AsyncMock(return_value=proposal))
    services = SimpleNamespace(db=db, runtime=runtime, provider=provider, config=config)
    return SimpleNamespace(
        db=db,
        task=task,
        turn=turn,
        job=job,
        actor=actor,
        ledger=ledger,
        bound=bound,
        services=services,
        proposal=proposal,
    )


@pytest.mark.parametrize(
    "change",
    [
        "task",
        "lease",
        "plan",
        "objective",
        "recipient",
        "actor",
        "committed",
        "revision",
        "privacy",
        "world",
        "excerpt",
    ],
)
def test_feedback_invalidates_when_source_or_scope_changes(recovery_case, change):
    c = recovery_case
    with c.db.session() as s:
        assert c.bound.current(s, c.task, c.turn, c.ledger)
    task, ledger, bound = c.task, deepcopy(c.ledger), c.bound
    if change == "task":
        bound = replace(bound, task_id="other")
    elif change == "lease":
        bound = replace(bound, lease_version=bound.lease_version + 1)
    elif change == "plan":
        ledger["revision"] += 1
    elif change == "objective":
        ledger["plan"]["jobs"][0]["objective"] += "其他内容"
    elif change == "recipient":
        ledger["plan"]["jobs"][0]["addressees"] = []
    elif change == "actor":
        ledger["plan"]["jobs"][0]["actor"] = c.turn.actor_ids[1]
    elif change == "committed":
        ledger["candidates"] = {c.job.id: {"status": "committed"}}
    elif change == "excerpt":
        bound = replace(bound, notice=bound.notice.model_copy(deep=True))
        bound.notice.rejected_premises[0].text = "私有证据"
    else:
        with c.db.session.begin() as s:
            actor = s.get(Conversation, c.actor.id)
            if change == "revision":
                actor.revision += 1
            elif change == "privacy":
                actor.privacy_version += 1
            else:
                s.get(Story, c.turn.story_id).seq += 1
    with c.db.session() as s:
        assert not bound.current(s, task, c.turn, ledger)


@pytest.mark.parametrize("stale", [False, True])
async def test_installed_recovery_uses_safe_notice_or_generic_fallback(recovery_case, stale):
    c = recovery_case
    if stale:
        with c.db.session.begin() as s:
            s.get(Conversation, c.actor.id).privacy_version += 1
    with c.db.session() as s:
        speech_before = list(s.query(SceneRecord.id).all())
    updated = await prepare_scene_plan(
        c.services, c.task, c.turn, recovery_reason="assignment_rejected", recovery_notice=c.bound
    )
    assert updated["revision"] == 2 and updated["recovery_count"] == 1
    c.services.provider.chat.assert_awaited_once()
    payload = json.loads(c.services.provider.chat.call_args.args[2][1]["content"])
    assert payload["recovery_feedback"] == (None if stale else c.bound.notice.model_dump())
    assert payload["recovery_reason"] == "assignment_rejected"
    with c.db.session() as s:
        assert speech_before == list(s.query(SceneRecord.id).all())  # No claimed world facts or speech.


async def test_privacy_change_during_model_call_prevents_install(recovery_case):
    c = recovery_case

    async def change_private_scope(*_args, **_kwargs):
        with c.db.session.begin() as s:
            s.get(Conversation, c.actor.id).privacy_version += 1
        return c.proposal

    c.services.provider.chat.side_effect = change_private_scope
    with pytest.raises(StaleTask):
        await prepare_scene_plan(
            c.services, c.task, c.turn, recovery_reason="assignment_rejected", recovery_notice=c.bound
        )
    c.services.provider.chat.assert_awaited_once()
    with c.db.session() as s:
        assert s.get(Operation, f"{c.task.id}:director-ledger").payload["revision"] == 1


async def test_execute_plan_filters_locally_repaired_private_excerpt_again(recovery_case, monkeypatch):
    from chatbot.npc import directed_turn

    c = recovery_case
    error = AssignmentRejected(
        "拒绝",
        rejected_premises=(
            RejectedPremise(text="他想聊公告哪部分", premise_kind="world_fact"),
            RejectedPremise(text="私有修复稿中的经历", premise_kind="past_action"),
        ),
    )
    monkeypatch.setattr(directed_turn, "authorize_assignment", AsyncMock(side_effect=error))
    performer = AsyncMock()
    monkeypatch.setattr(directed_turn, "perform_actor", performer)
    with pytest.raises(AssignmentRejected) as caught:
        await directed_turn.execute_plan(c.services, c.task, c.turn, c.ledger, None)
    assert caught.value.recovery_notice.notice.rejected_premises == [error.rejected_premises[0]]
    assert "私有" not in caught.value.recovery_notice.notice.model_dump_json()
    performer.assert_not_called()


async def test_recoverable_forwards_notice_and_still_executes_authorized_plan(recovery_case, monkeypatch):
    from chatbot.npc import directed_scene
    from chatbot.npc.director import snapshot

    c = recovery_case
    failure = AssignmentRejected("拒绝")
    failure.recovery_notice = c.bound
    execute = AsyncMock(side_effect=[failure, {"stop_reason": "player_turn"}])
    monkeypatch.setattr(directed_scene, "execute_plan", execute)
    with c.db.session() as s:
        before = snapshot(s, c.task, c.turn)
    updated, result = await directed_scene.execute_recoverable(
        c.services,
        c.task,
        c.turn,
        c.ledger,
        None,
        before,
    )
    assert updated["recovery_count"] == 1 and result["stop_reason"] == "player_turn"
    assert execute.await_count == 2
    assert execute.call_args.args[3] == updated
    payload = json.loads(c.services.provider.chat.call_args.args[2][1]["content"])
    assert payload["recovery_feedback"] == c.bound.notice.model_dump()


async def test_feedback_changes_cache_identity_and_survives_scope_retry():
    from test_director_planning import arguments, provider

    from chatbot.director_planning import propose_plan

    p, args = provider(), arguments()
    args["recovery_reason"] = "assignment_rejected"
    notice = RecoveryNotice(actor="a", job_id="old", rejected_premises=[RejectedPremise(text="想谈公告")])
    await propose_plan(p, **args)
    original_key = p.chat.call_args.kwargs["operation"]
    await propose_plan(p, **args, recovery_feedback=notice)
    notice_key = p.chat.call_args.kwargs["operation"]
    assert notice_key != original_key
    await propose_plan(p, **args, recovery_feedback=notice)
    assert notice_key == p.chat.call_args.kwargs["operation"]
    valid = deepcopy(p.chat.return_value)
    invalid = deepcopy(valid)
    invalid["jobs"][0]["actor"] = "outsider"
    p.chat.reset_mock()
    p.chat.side_effect = [invalid, valid]
    await propose_plan(p, **args, recovery_feedback=notice)
    assert p.chat.await_count == 2
    for call in p.chat.call_args_list:
        payload = json.loads(call.args[2][1]["content"])
        assert payload["recovery_feedback"] == notice.model_dump()
    assert p.chat.call_args_list[0].kwargs["operation"] != p.chat.call_args_list[1].kwargs["operation"]


@pytest.mark.parametrize("reason,actor", [("", "a"), ("assignment_rejected", "outsider")])
async def test_feedback_cannot_enter_unrelated_planning(reason, actor):
    from test_director_planning import arguments, provider

    from chatbot.director_planning import propose_plan

    p, args = provider(), arguments()
    args["recovery_reason"] = reason
    notice = RecoveryNotice(actor=actor, job_id="old", rejected_premises=[RejectedPremise(text="想谈公告")])
    with pytest.raises(ValueError, match="诊断"):
        await propose_plan(p, **args, recovery_feedback=notice)
    p.chat.assert_not_called()


@pytest.mark.parametrize("own_ack", [True, False])
def test_only_verified_own_ack_can_advance_feedback_world_scope(recovery_case, own_ack):
    from chatbot.observations import append_record

    c = recovery_case
    with c.db.session.begin() as s:
        append_record(
            s,
            story_id=c.turn.story_id,
            kind="ack_candidate",
            text="嗯。",
            actor=s.get(Conversation, c.actor.id),
            observer_ids=[],
            task_id=c.task.id if own_ack else "different-task",
            dedupe="feedback-ack",
            payload={"acknowledgement": True},
        )
    with c.db.session() as s:
        assert c.bound.current(s, c.task, c.turn, c.ledger) is own_ack


async def test_final_install_rechecks_private_scope_under_actor_lock(recovery_case, monkeypatch):
    from chatbot.npc import director

    c = recovery_case
    original_install = director.install_plan

    def change_before_install(*args, **kwargs):
        with c.db.session.begin() as s:
            s.get(Conversation, c.actor.id).privacy_version += 1
        return original_install(*args, **kwargs)

    monkeypatch.setattr(director, "install_plan", change_before_install)
    with pytest.raises(StaleTask):
        await prepare_scene_plan(
            c.services, c.task, c.turn, recovery_reason="assignment_rejected", recovery_notice=c.bound
        )
    c.services.provider.chat.assert_awaited_once()
    with c.db.session() as s:
        assert s.get(Operation, f"{c.task.id}:director-ledger").payload["revision"] == 1
