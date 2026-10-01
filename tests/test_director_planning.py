from unittest.mock import AsyncMock

import pytest

from chatbot.directing import SceneDirection
from chatbot.director_planning import RecoveryPlanIncomplete, propose_plan
from chatbot.runtime import StaleTask


def arguments():
    return dict(
        task_id="task",
        direction=SceneDirection(
            objective="澄清误会",
            beats=[dict(id="b", title="交谈", objective="询问", participants=["a"], completion="玩家回应")],
        ),
        beat_id="b",
        scene_revision="1",
        public_context="a 在大厅",
        eligible={"a"},
        visible_to={"a": {"player"}},
        is_current=lambda: True,
    )


def provider():
    from types import SimpleNamespace

    return SimpleNamespace(
        chat=AsyncMock(
            return_value=dict(
                beat_id="b",
                after_plan="yield_player",
                remaining_work=[],
                reason="询问玩家",
                jobs=[dict(id="j", actor="a", objective="询问", addressees=["player"])],
            )
        )
    )


@pytest.mark.asyncio
async def test_plan_cache_is_bound_to_inputs_and_uses_budgeted_provider():
    p = provider()
    args = arguments()
    result = await propose_plan(p, **args)
    assert result.jobs[0].actor == "a"
    first = p.chat.call_args
    assert first.args[:2] == ("task", "director")
    await propose_plan(p, **args)
    assert first.kwargs["operation"] == p.chat.call_args.kwargs["operation"]
    args["public_context"] += "，玩家走入大厅"
    await propose_plan(p, **args)
    assert first.kwargs["operation"] != p.chat.call_args.kwargs["operation"]
    previous = p.chat.call_args.kwargs["operation"]
    args["completed_jobs"] = [{"actor": "a", "objective": "已提出建议", "event_id": "committed"}]
    await propose_plan(p, **args)
    assert previous != p.chat.call_args.kwargs["operation"]
    import json

    payload = json.loads(p.chat.call_args.args[2][1]["content"])
    assert payload["completed_jobs"] == args["completed_jobs"]


@pytest.mark.asyncio
async def test_revalidate_scope_even_when_provider_returns_cached_plan():
    p = provider()
    args = arguments()
    args["visible_to"] = {"a": set()}
    with pytest.raises(ValueError, match="可感知"):
        await propose_plan(p, **args)
    assert p.chat.await_count == 2


@pytest.mark.asyncio
async def test_invisible_secondary_addressee_is_narrowed_without_new_model_call():
    p = provider()
    p.chat.return_value["jobs"][0]["addressees"] = ["player", "unnoticed-npc"]
    result = await propose_plan(p, **arguments())
    assert result.jobs[0].addressees == ["player"]
    p.chat.assert_awaited_once()


async def test_oversize_plan_regenerated_once_without_silent_truncation():
    p = provider()
    valid = p.chat.return_value
    invalid = {**valid, "jobs": [dict(valid["jobs"][0], id=f"j{i}") for i in range(4)]}
    p.chat.side_effect = [invalid, valid]
    result = await propose_plan(p, **arguments())
    assert [job.id for job in result.jobs] == ["j"]
    first, second = p.chat.call_args_list
    assert first.kwargs["operation"] != second.kwargs["operation"]
    import json

    before, after = [json.loads(call.args[2][1]["content"]) for call in (first, second)]
    for field in ("beat", "eligible", "visible_to", "public_context", "scene_revision"):
        assert before[field] == after[field]
    assert "最多3个job" in after["recovery_reason"]


async def test_scope_repair_rechecks_cancellation_before_another_paid_call():
    p = provider()
    args = arguments()
    args["visible_to"] = {"a": set()}
    states = iter([True, True, False])
    args["is_current"] = lambda: next(states)
    with pytest.raises(StaleTask):
        await propose_plan(p, **args)
    p.chat.assert_awaited_once()


@pytest.mark.parametrize("dependency", ["j", "a", "old-job"])
async def test_invalid_dependency_uses_bounded_repair(dependency):
    p = provider()
    valid = p.chat.return_value
    invalid = {**valid, "jobs": [{**valid["jobs"][0], "depends_on": [dependency]}]}
    p.chat.side_effect = [invalid, valid]
    result = await propose_plan(p, **arguments())
    assert result.jobs[0].depends_on == []
    assert p.chat.await_count == 2


async def test_repeated_invalid_dependency_stops_after_one_repair():
    p = provider()
    p.chat.return_value["jobs"][0]["depends_on"] = ["j"]
    with pytest.raises(ValueError, match="依赖"):
        await propose_plan(p, **arguments())
    assert p.chat.await_count == 2


async def test_recovery_replans_when_it_drops_a_previously_scheduled_speaker():
    p = provider()
    args = arguments()
    args["direction"].beats[0].participants = ["a", "b"]
    args["eligible"] = {"a", "b"}
    args["visible_to"]["b"] = {"player"}
    args["required_recovery_actors"] = ["a", "b"]
    args["recovery_reason"] = "draft_rejected"
    complete = {
        **p.chat.return_value,
        "jobs": [
            dict(id="j", actor="a", objective="说出已知部分", addressees=["player"]),
            dict(id="k", actor="b", objective="说出已知部分", addressees=["player"]),
        ],
    }
    p.chat.side_effect = [p.chat.return_value, complete]
    plan = await propose_plan(p, **args)
    assert [job.actor for job in plan.jobs] == ["a", "b"]
    assert p.chat.await_count == 2
    import json

    retry = json.loads(p.chat.call_args.args[2][1]["content"])
    assert retry["required_recovery_actors"] == ["a", "b"]


async def test_recovery_never_calls_missing_speakers_completed():
    p = provider()
    args = arguments()
    args["direction"].beats[0].participants = ["a", "b"]
    args["eligible"] = {"a", "b"}
    args["visible_to"]["b"] = {"player"}
    args["required_recovery_actors"] = ["a", "b"]
    with pytest.raises(RecoveryPlanIncomplete, match="b"):
        await propose_plan(p, **args)
    assert p.chat.await_count == 2


@pytest.mark.asyncio
async def test_no_eligible_actors_needs_no_model_call():
    p = provider()
    args = arguments()
    args["eligible"] = set()
    result = await propose_plan(p, **args)
    assert result.handoff and not result.jobs
    p.chat.assert_not_called()


@pytest.mark.asyncio
async def test_stale_before_and_after_planning():
    p = provider()
    args = arguments()
    args["is_current"] = lambda: False
    with pytest.raises(StaleTask):
        await propose_plan(p, **args)
    p.chat.assert_not_called()
    checks = iter([True, False])
    args["is_current"] = lambda: next(checks)
    with pytest.raises(StaleTask):
        await propose_plan(p, **args)
    p.chat.assert_awaited_once()


def test_continuation_requires_actionable_work_and_preserves_ledger_contract():
    from pydantic import ValidationError

    from chatbot.directing import DirectorPlan
    from chatbot.director_planning import PlanProposal

    raw = dict(
        beat_id="b",
        jobs=[dict(id="j", actor="a", objective="回应NPC")],
        after_plan="continue_scene",
        remaining_work=["询问玩家"],
        reason="先完成回应",
    )
    proposal = PlanProposal.model_validate(raw)
    plan = proposal.to_plan()
    assert not plan.handoff
    assert DirectorPlan.model_validate(plan.model_dump()) == plan
    assert "after_plan" not in plan.model_dump()
    for invalid in ({"jobs": []}, {"remaining_work": []}, {"remaining_work": [" "]}):
        with pytest.raises(ValidationError):
            PlanProposal.model_validate({**raw, **invalid})
    yielded = PlanProposal.model_validate(
        {**raw, "jobs": [], "after_plan": "yield_player", "remaining_work": []}
    )
    assert yielded.to_plan().handoff
