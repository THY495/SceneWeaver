"""Only scheduling width changes; actual dependency commits remain mandatory."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from support.story import launch

from chatbot.api import create_app
from chatbot.config import Settings
from chatbot.directing import DirectorPlan
from chatbot.director_state import install_plan
from chatbot.npc.context import TurnContext
from chatbot.npc.directed_turn import execute_plan
from chatbot.storage import Conversation, SceneRecord, Story


def test_scheduling_width_is_bounded_and_default_unchanged():
    assert Settings(_env_file=None).role_preparation_concurrency == 2
    for width in [0, 3]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, role_preparation_concurrency=width)


async def test_frozen_plan_has_two_ready_jobs_and_real_dependency(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from probe_preparation_concurrency import fixed_plan, fixture

    cast = [{"id": key, "name": name} for key, name in zip(["a", "s", "l", "p"], ["阿岚", "沈舟", "林澈", "旅人"])]
    kwargs = {"public_context": json.dumps({"cast": cast}), "current_player_event_id": "real-player-input"}
    plan = await fixed_plan(None, "task", None, "beat", **kwargs)
    assert [job.depends_on for job in plan.jobs] == [[], [], ["alan", "shen"]]
    assert plan.jobs[2].grounded_response.job_id == "shen"
    assert plan.jobs[0].grounded_response.event_id == plan.jobs[1].grounded_response.event_id == "real-player-input"
    assert fixture()["narrative"]["nodes"][0]["direction"]["beats"][0]["max_segments"] == 3
    recovery = await fixed_plan(None, "task", None, "beat", recovery_reason="draft_rejected", **kwargs)
    assert not recovery.jobs  # failures do not draw a new performance plan


def test_completed_empty_plan_is_not_completed_workload(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from probe_preparation_concurrency import workload_complete

    report = {"status": "completed", "stop_reason": "player_turn", "segments": [], "source_authorizations": []}
    assert not workload_complete(report, {"a", "s", "l"})
    report["segments"] = [{"actor": name, "actor_id": key, "event_id": "event-" + key}
        for name, key in [("阿岚", "a"), ("沈舟", "s"), ("林澈", "l")]]
    assert not workload_complete(report, {"a", "s", "l"})
    report["source_authorizations"] = [{"source_actor_id": "s", "response_actor_id": "l",
        "source_event_id": "event-s", "response_event_id": "event-l", "verified_committed_link": True}]
    assert workload_complete(report, {"a", "s", "l"})
    report["source_authorizations"][0]["source_event_id"] = "historical-other-task"
    assert not workload_complete(report, {"a", "s", "l"})


@pytest.mark.parametrize("width", [1, 2])
async def test_real_scheduler_width_and_dependency_commit(db, config, runtime, fake, monkeypatch, width):
    from fastapi.testclient import TestClient

    from chatbot.npc import directed_turn

    config.product = "roleplay"
    config.role_preparation_concurrency = width
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "各自说明，再由第三位回应", "width")
    task = runtime.claim("test")
    with db.session() as s:
        seq = s.get(Story, sid).seq
    ledger = install_plan(db, task, sid, seq, DirectorPlan(beat_id="b", reason="冻结依赖", jobs=[
        {"id": "a", "actor": actors[0], "objective": "独立表达"},
        {"id": "b", "actor": actors[1], "objective": "独立表达"},
        {"id": "c", "actor": actors[2], "objective": "实际回应", "depends_on": ["a", "b"]},
    ]))
    entered, release, second = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started = []

    async def plan(services, task, turn, actor, **kwargs):
        started.append(actor.id)
        if actor.id == actors[2]:
            with db.session() as s:
                formal = list(s.query(SceneRecord).filter_by(task_id=task.id, kind="speech").all())
                assert {r.actor_id for r in formal} >= set(actors[:2])
            assert len(kwargs["replies"]) == 2
        elif actor.id == actors[0]:
            entered.set()
            await asyncio.wait_for(release.wait(), 2)
        else:
            second.set()
        return {"act": "speak", "purpose": "表达", "addressee_id": player, "expects_reply": False}

    async def perform(services, task, turn, actor, intent, step, **kwargs):
        with db.session() as s:
            revision = s.get(Conversation, actor.id).revision
        return {"answer": "这是我的看法。", "revision": revision}

    monkeypatch.setattr(directed_turn, "authorize_assignment", AsyncMock(return_value={"objective": "冻结任务"}))
    monkeypatch.setattr(directed_turn, "plan_actor", plan)
    monkeypatch.setattr(directed_turn, "perform_actor", perform)
    future = asyncio.create_task(execute_plan(SimpleNamespace(db=db, runtime=runtime, config=config), task,
        TurnContext(sid, player, tuple(actors)), ledger, None))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if width == 2:
            await asyncio.wait_for(second.wait(), 2)
        else:
            await asyncio.sleep(0)
            assert not second.is_set()
        assert actors[2] not in started
        release.set()
        result = await asyncio.wait_for(future, 3)
        assert result["actor_ids"] == actors and len(result["segments"]) == 3
    finally:
        release.set()
        if not future.done():
            future.cancel()
        await asyncio.gather(future, return_exceptions=True)
