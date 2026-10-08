import pytest
from pydantic import ValidationError

from chatbot.directing import Beat, DirectorPlan, validate_plan
from chatbot.npc.exchange_requirement import npc_exchange_observed


def beat():
    return Beat(
        id="b",
        title="交换所知",
        objective="交谈",
        participants=["a", "b"],
        completion="交流发生",
        npc_exchange_required=True,
    )


def test_old_beats_are_free_and_draft_can_be_incomplete():
    value = beat().model_dump()
    value.pop("npc_exchange_required")
    assert Beat.model_validate(value).npc_exchange_required is False
    value.update(npc_exchange_required=True, participants=["a"])
    with pytest.raises(ValidationError, match="至少两位"):
        Beat.model_validate(value)
    assert Beat.model_validate(value, context={"authoring_draft": True}).npc_exchange_required


def test_all_player_reports_are_replanned_but_committed_exchange_is_not_repeated():
    plan = DirectorPlan(
        beat_id="b", reason="陈述", jobs=[dict(id="j", actor="a", objective="说明所知", addressees=["p"])]
    )
    with pytest.raises(ValueError, match="全部面向玩家"):
        validate_plan(plan, beat(), {"a", "b"}, {"a": {"b", "p"}})
    assert validate_plan(plan, beat(), {"a", "b"}, {"a": {"b", "p"}}, npc_exchange_observed=True) == plan
    plan.jobs[0].addressees = ["b"]
    assert validate_plan(plan, beat(), {"a", "b"}, {"a": {"b", "p"}}) == plan


@pytest.mark.parametrize(
    "kind,actor,target,current,expected",
    [
        ("speech", "a", "b", True, True),
        ("speech", "a", "p", True, False),
        ("speech", "a", "a", True, False),
        ("speech", "p", "a", True, False),
        ("action", "a", "b", True, False),
        ("speech", "a", "b", False, False),
    ],
)
def test_only_current_actual_npc_addressing_counts(kind, actor, target, current, expected):
    events = [
        {"event_id": "e", "kind": kind, "actor_id": actor, "addressees": [target], "text": "乙，你听见了吗？"}
    ]
    assert npc_exchange_observed(beat(), events, {"e"} if current else set()) is expected
    assert npc_exchange_observed(beat(), [], {"e"}) is False


@pytest.mark.parametrize("npc_target", [True, False])
@pytest.mark.parametrize("recorded_beat", ["b", "previous-beat"])
async def test_model_met_verdict_cannot_complete_beat_without_actual_npc_exchange(
    db, config, runtime, fake, npc_target, recorded_beat
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient
    from support.story import launch

    from chatbot.api import create_app
    from chatbot.npc.beat_progress import assess_beat
    from chatbot.npc.context import TurnContext
    from chatbot.observations import append_record
    from chatbot.storage import Conversation, ExperimentInitial, Operation, Story

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
                request_id="exchange",
                request_hash="exchange",
                receipt={"actors": {"a": actors[0], "b": actors[1]}},
                recipe={
                    "people": [],
                    "narrative": {
                        "nodes": [
                            {
                                "id": "room",
                                "direction": {"objective": "交换意见", "beats": [beat().model_dump()]},
                            }
                        ]
                    },
                },
            )
        )
    runtime.submit(player, config.workspace_owner, "你们互相说说。", "exchange-turn")
    task = runtime.claim("test")
    with db.session.begin() as s:
        row = append_record(
            s,
            story_id=sid,
            actor=s.get(Conversation, actors[0]),
            kind="speech",
            text="我目前还不知道。",
            addressees=[actors[1] if npc_target else player],
            observer_ids=[*actors, player],
            dedupe="exchange:actual",
            task_id=task.id,
        )
        event_id = row.id
        s.add(
            Operation(
                id=f"{task.id}:director-ledger",
                task_id=task.id,
                payload={
                    "plan": {"beat_id": recorded_beat},
                    "candidates": {"job": {"status": "committed", "event_id": event_id}},
                },
            )
        )
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value={
                "status": "met",
                "reason": "交流已发生",
                "evidence": [{"event_id": event_id, "quote": "我目前还不知道。"}],
            }
        )
    )
    result = await assess_beat(
        SimpleNamespace(db=db, provider=provider), task, TurnContext(sid, player, tuple(actors)), "b"
    )
    assert result["assessment"]["status"] == "met"
    expected = npc_target and recorded_beat == "b"
    assert result["npc_exchange_met"] is expected
    assert result["advanced"] is expected
    with db.session() as s:
        completed = s.get(Story, sid).settings["narrative"].get("direction_progress", {}).get("completed", [])
        assert ("b" in completed) is expected
