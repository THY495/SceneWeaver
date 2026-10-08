"""Source task adoption cannot erase author requirements or publish stale grants."""

from copy import deepcopy

import pytest
from test_combined_actor_generation import actor_case as actor_case
from test_director_planning import arguments, provider
from test_grounded_assignment import authorize, install

from chatbot.director_planning import propose_plan
from chatbot.director_state import CandidateCommit, check_publication, save_candidate
from chatbot.npc.assignment import AssignmentRejected
from chatbot.npc.grounded_assignment import compile_response, record_authorization
from chatbot.runtime import StaleTask
from chatbot.source_authorization import author_source_scope, permits_source_tasks
from chatbot.storage import ExperimentInitial, Operation, Story, Task


def change(case, kind):
    with case.services.db.session.begin() as s:
        origin = s.get(ExperimentInitial, case.turn.story_id)
        recipe = deepcopy(origin.recipe)
        direction = recipe["narrative"]["nodes"][0]["direction"]
        beat = direction["beats"][0]
        if kind == "default":
            beat.pop("source_response_tasks")
        elif kind == "scene":
            direction["constraints"] = ["每条台词至少三句，并在第三句收束。"]
        elif kind == "action":
            beat["constraints"] = ["本段须先提出递交信件，不能直接跳到回应。"]
        elif kind == "stance":
            beat["constraints"] = ["让双方讨论争议后才交还玩家。"]
        elif kind == "secret":
            beat["constraints"] = ["作者秘密：抽屉内有第二份遗嘱；不能直接告知角色。"]
        elif kind == "focus":
            beat["role_focus"] = {case.actor.id: "问清玩家来意"}
        origin.recipe = recipe


@pytest.mark.parametrize("kind", ["default", "scene", "action", "stance", "secret", "focus"])
async def test_author_requirements_never_enter_unreviewed_compiled_assignment(actor_case, fake, kind):
    case = actor_case
    job, _ = install(case)
    case.services.config.role_assignment_authorization = "grounded_response"
    change(case, kind)
    with pytest.raises(AssignmentRejected, match="作者许可"):
        await authorize(case, job)
    assert not fake.calls  # Actor never receives discarded constraints or private author data.


async def test_legacy_semantic_comparison_remains_available(actor_case, fake):
    case = actor_case
    job, _ = install(case)
    change(case, "default")
    case.services.config.role_assignment_authorization = "semantic"
    fake.replies["director_assignment"] = {"checks": [], "accepted": True, "issues": []}
    await authorize(case, job)
    assert [role for role, _, _ in fake.calls] == ["director_assignment"]


def test_scope_default_and_explicit_opt_in():
    direction = arguments()["direction"]
    beat = direction.beats[0]
    assert not permits_source_tasks(direction, beat)
    beat.source_response_tasks = True
    assert permits_source_tasks(direction, beat)
    beat.constraints = ["特殊表达要求"]
    assert not permits_source_tasks(direction, beat)


async def test_real_director_generation_permission_is_bound_and_enforced():
    p = provider()
    args = arguments()
    args["grounded_responses"] = True
    await propose_plan(p, **args)
    import json

    assert not json.loads(p.chat.call_args.args[2][1]["content"])["grounded_responses"]
    p.chat.return_value["jobs"] = [{"kind": "source", "id": "j", "actor": "a",
        "addressees": ["player"], "grounded_response": {"event_id": "e"}}]
    with pytest.raises(ValueError, match="未授权"):
        await propose_plan(p, **args)
    args["direction"].beats[0].source_response_tasks = True
    assert (await propose_plan(p, **args)).jobs[0].grounded_response.event_id == "e"


def test_revocation_between_compile_and_authorization_is_rejected(actor_case):
    case = actor_case
    case.services.config.role_assignment_authorization = "grounded_response"
    job, _ = install(case)
    _, info = case.services.context.assemble(case.task, actor_id=case.actor.id)
    assignment, proof = compile_response(case.services, case.task, case.turn, case.actor, job, info)
    change(case, "default")
    with pytest.raises(StaleTask):
        record_authorization(case.services, case.task, case.actor, job, assignment, proof, "test:")


@pytest.mark.parametrize("mutation", ["revoke", "audit_deleted"])
async def test_author_revocation_after_review_blocks_new_publication_but_preserves_replay(actor_case, fake, mutation):
    case = actor_case
    case.services.config.role_assignment_authorization = "grounded_response"
    job, ledger = install(case)
    await authorize(case, job)
    value = {"text": "已审查的回应"}
    row = save_candidate(case.services.db, case.task, case.turn.story_id, ledger["world_seq"],
        plan_revision=ledger["revision"], job_id=job.id, expected_revision=0,
        dependency_revisions={}, candidate=value)
    token = CandidateCommit(ledger["revision"], job.id, row["revision"])
    with case.services.db.session() as s:
        check_publication(s, case.task, s.get(Story, case.turn.story_id), token,
                          step=0, actor_id=case.actor.id, value=value)
    if mutation == "revoke":
        change(case, "secret")
    else:
        with case.services.db.session.begin() as s:
            op = s.get(Operation, f"{case.task.id}:director-ledger")
            s.delete(s.get(Operation, op.payload["authorizations"][job.id]["operation_id"]))
    with case.services.db.session() as s, pytest.raises(StaleTask, match="作者许可"):
        check_publication(s, case.task, s.get(Story, case.turn.story_id), token,
                          step=0, actor_id=case.actor.id, value=value)
    with case.services.db.session.begin() as s:
        op = s.get(Operation, f"{case.task.id}:director-ledger")
        data = deepcopy(op.payload)
        data["candidates"][job.id].update(status="committed", event_id="already-published")
        op.payload = data
    with case.services.db.session() as s:
        check_publication(s, case.task, s.get(Story, case.turn.story_id), token,
                          step=0, actor_id=case.actor.id, value=value, event_id="already-published")


def test_direct_permission_is_exact_frame_not_general_source_grant(actor_case):
    case = actor_case
    job, ledger = install(case)
    change(case, "default")
    with case.services.db.session.begin() as s:
        origin = s.get(ExperimentInitial, case.turn.story_id)
        recipe = deepcopy(origin.recipe)
        beat = recipe["narrative"]["nodes"][0]["direction"]["beats"][0]
        beat.update(participants=[case.actor.id], direct_response_actor=case.actor.id, max_segments=1)
        origin.recipe = recipe
        assert author_source_scope(s, case.task, case.actor.id, job, ledger) is None
        s.add(Operation(id=f"{case.task.id}:direct-response-frame", task_id=case.task.id,
            payload={"plan_revision": ledger["revision"], "actor_id": case.actor.id,
                     "source_id": case.task.payload["event_id"]}))
        s.flush()
        assert author_source_scope(s, case.task, case.actor.id, job, ledger)
        job.grounded_response.purpose = "invite_player"
        assert author_source_scope(s, case.task, case.actor.id, job, ledger) is None
        job.grounded_response.purpose = "respond"
        live = s.get(Task, case.task.id)
        for fields in [{"channel": "private"}, {"autonomous": True}]:
            original = live.payload
            live.payload = {**original, **fields}
            assert author_source_scope(s, case.task, case.actor.id, job, ledger) is None
            live.payload = original
