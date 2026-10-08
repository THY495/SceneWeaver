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
                jobs=[dict(kind="free", id="j", actor="a", objective="询问", addressees=["player"])],
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
    from chatbot.directing import DirectorPlan
    from chatbot.director_planning import narrow_addressees

    # Legacy runtime plans remain readable/narrowable; generation itself
    # rejects multiple primary recipients before conversion.
    result = narrow_addressees(DirectorPlan(beat_id='b', reason='legacy', jobs=[
        dict(id='j', actor='a', objective='询问', addressees=['player', 'unnoticed-npc'])
    ]), {'a': {'player'}})
    assert result.jobs[0].addressees == ["player"]


@pytest.mark.asyncio
async def test_two_visible_addressees_are_replanned_as_one_primary_recipient():
    p = provider()
    args = arguments()
    args["visible_to"] = {"a": {"player", "b"}}
    invalid = {
        **p.chat.return_value,
        "jobs": [dict(kind="free", id="j", actor="a", objective="回应 b", addressees=["player", "b"])],
    }
    valid = {**p.chat.return_value, "jobs": [dict(kind="free", id="j", actor="a", objective="回应 b", addressees=["b"])]}
    p.chat.side_effect = [invalid, valid]
    plan = await propose_plan(p, **args)
    assert plan.jobs[0].addressees == ["b"]
    assert p.chat.await_count == 2


async def test_explicit_npc_exchange_requirement_replans_all_player_reports_once():
    p = provider()
    args = arguments()
    args["direction"].beats[0].participants = ["a", "b"]
    args["direction"].beats[0].npc_exchange_required = True
    args["eligible"] = {"a", "b"}
    args["visible_to"] = {"a": {"player", "b"}, "b": {"player", "a"}}
    invalid = p.chat.return_value
    valid = {**invalid, "jobs": [dict(kind="free", id="j", actor="a", objective="向乙说明所知", addressees=["b"])]}
    p.chat.side_effect = [invalid, valid]
    plan = await propose_plan(p, **args)
    assert plan.jobs[0].addressees == ["b"]
    assert p.chat.await_count == 2
    assert "作者要求NPC之间直接交流" in p.chat.await_args.args[2][1]["content"]


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


async def test_named_reply_to_new_speech_requires_dependency_before_actor_preparation():
    import json

    p = provider()
    args = arguments()
    args["direction"].beats[0].participants = ["a", "b", "c"]
    args["eligible"] = {"a", "b", "c"}
    args["visible_to"] = {key: {"a", "b", "c", "player"} for key in ("a", "b", "c")}
    args["public_context"] = json.dumps(
        {"cast": [{"id": "a", "name": "阿岚"}, {"id": "b", "name": "沈舟"}, {"id": "c", "name": "林澈"}]}
    )
    invalid = {
        **p.chat.return_value,
        "jobs": [
            dict(kind="free", id="alan", actor="a", objective="说出自己的看法"),
            dict(kind="free", id="shen", actor="b", objective="回应阿岚的表态"),
            dict(kind="free", id="lin", actor="c", objective="回应阿岚和沈舟的说法"),
        ],
    }
    p.chat.return_value = invalid
    plan = await propose_plan(p, **args)
    assert [job.depends_on for job in plan.jobs] == [[], ["alan"], ["shen"]]
    p.chat.assert_awaited_once()


async def test_independent_multi_actor_opinions_still_prepare_in_parallel():
    import json

    p = provider()
    args = arguments()
    args["direction"].beats[0].participants = ["a", "b"]
    args["eligible"] = {"a", "b"}
    args["visible_to"]["b"] = {"player"}
    args["public_context"] = json.dumps({"cast": [{"id": "a", "name": "阿岚"}, {"id": "b", "name": "沈舟"}]})
    p.chat.return_value["jobs"] = [
        dict(kind="free", id="alan", actor="a", objective="独立表达自己的看法"),
        dict(kind="free", id="shen", actor="b", objective="独立表达自己的看法"),
    ]
    plan = await propose_plan(p, **args)
    assert [job.depends_on for job in plan.jobs] == [[], []]
    p.chat.assert_awaited_once()


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
            dict(kind="free", id="j", actor="a", objective="说出已知部分", addressees=["player"]),
            dict(kind="free", id="k", actor="b", objective="说出已知部分", addressees=["player"]),
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


async def test_director_wire_schema_bounds_primary_recipient_without_changing_plan_ledger(
    db, config, runtime
):
    import json

    import httpx

    from chatbot.directing import DirectorPlan
    from chatbot.providers import Provider

    session = runtime.create_session()
    runtime.submit(session.id, "local", "请他们互相回应", "director-schema")
    task = runtime.claim("worker")
    args = arguments()
    args["task_id"] = task.id
    value = provider().chat.return_value
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        schema = json.loads(payload["messages"][-1]["content"].split("：", 1)[1])
        jobs = schema["properties"]["jobs"]
        assert jobs['items']['discriminator']['propertyName'] == 'kind'
        job = schema["$defs"]['GeneratedFreeJob']
        assert job["properties"]["addressees"]["maxItems"] == 1
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    live = Provider(db, config, runtime)
    await live.http.aclose()
    live.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        plan = await propose_plan(live, **args)
        assert len(requests) == 1
        assert plan.jobs[0].addressees == ["player"]
        assert DirectorPlan.model_validate(plan.model_dump()) == plan
        assert "after_plan" not in plan.model_dump()
    finally:
        await live.close()


def test_new_generation_rejects_multiple_primary_recipients_but_legacy_reader_preserves_them():
    from pydantic import ValidationError

    from chatbot.director_planning import GeneratedPlanProposal, PlanProposal

    value = provider().chat.return_value
    value["jobs"][0]["addressees"] = ["player", "npc-b"]
    with pytest.raises(ValidationError) as error:
        GeneratedPlanProposal.model_validate(value)
    assert error.value.errors()[0]["loc"] == ("jobs", 0, "free", "addressees")
    del value['jobs'][0]['kind']
    assert PlanProposal.model_validate(value).to_plan().jobs[0].addressees == ["player", "npc-b"]


async def test_nonconforming_director_output_fails_with_two_requests_and_no_cached_plan(db, config, runtime):
    import json

    import httpx
    from sqlalchemy import select

    from chatbot.providers import Provider, StructuredOutputError
    from chatbot.storage import Operation, Usage

    session = runtime.create_session()
    runtime.submit(session.id, "local", "请他们互相回应", "director-invalid-schema")
    task = runtime.claim("worker")
    args = arguments()
    args.update(task_id=task.id, visible_to={"a": {"player", "npc-b"}})
    value = provider().chat.return_value
    value["jobs"][0]["addressees"] = ["player", "npc-b"]
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            },
        )

    live = Provider(db, config, runtime)
    await live.http.aclose()
    live.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(StructuredOutputError):
            await propose_plan(live, **args)
        assert len(requests) == 2
        # Keep the original scene/rules/schema. The existing second attempt
        # now receives content-free structural feedback instead of a blind retry.
        assert requests[0]["messages"][:2] == requests[1]["messages"][:2]
        assert requests[0]["messages"][-1] == requests[1]["messages"][-1]
        feedback = json.loads(requests[1]["messages"][2]["content"])
        assert feedback['format_errors'][0] == {'path': 'jobs.0.free.addressees', 'type': 'too_long'}
        with db.session() as store:
            assert not any("director-plan-v21:" in row.id for row in store.scalars(select(Operation)))
            usage = list(store.scalars(select(Usage)))
            assert len(usage) == 2
            assert all(row.reserved == 0 for row in usage)
    finally:
        await live.close()
