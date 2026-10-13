"""Real context pressure should be relieved by ordinary background Worker lanes."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention, launch

from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.providers import BudgetExceeded, Provider
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, RoleSnapshot, SceneRecord, Task, now
from chatbot.worker import Worker


@pytest.mark.parametrize("hard,repeat,success", [(16000, 85, True), (12000, 110, False)])
async def test_urgent_memory_catches_up_more_than_one_prefix_for_running_turn(
    db, config, fake, hard, repeat, success
):
    config.product = "roleplay"
    # A reduced test capacity isolates the real pressure branch. This is NOT
    # evidence that default-capacity natural conversation reached the limit.
    config.context_soft_tokens = 4000
    config.context_hard_tokens = hard
    fake.replies["role_memory"] = {"summary": "角色整理了自己的旧记录；具体内容以原始观察为准。", "notes": []}
    with TestClient(create_app(config, db, fake, serve_web=False)) as client:
        story, actors, player = launch(client)
    keeper = actors[0]
    # Valid-length, compressible speeches, not a large mandatory profile or
    # input. No schedule(force=True), artificial pressure exception or claim.
    for index in range(7):
        with db.session.begin() as s:
            append_record(
                s,
                story_id=story,
                kind="speech",
                text=f"旧记录{index}：" + "记录仅表示角色当时的发言。" * repeat,
                actor=s.get(Conversation, keeper),
                observer_ids=[keeper],
                dedupe=f"urgent-catchup-{index}",
            )
    fake.replies["role_intent"] = intention(player)
    fake.replies["dialogue"] = {"text": "你好，我愿意继续听你说。", "claims": []}
    fake.replies["actor_review"] = {"accepted": True, "issues": []}
    worker = Worker(db, config, fake)
    submitted = worker.runtime.submit(
        player, config.workspace_owner, "馆长，请向我问好。", "urgent-pressure", addressees=[keeper]
    )
    lanes = [
        asyncio.create_task(worker.lane(0, False)),
        asyncio.create_task(worker.lane(config.worker_concurrency - 1, True)),
    ]
    try:
        async with asyncio.timeout(12):
            while True:
                with db.session() as s:
                    task = s.get(Task, submitted.id)
                    if task.status in {"completed", "failed", "cancelled", "superseded"}:
                        break
                await asyncio.sleep(0.05)
        with db.session() as s:
            before = [
                (t.status, t.payload.get("cut"), t.payload.get("urgent"), t.result)
                for t in s.scalars(
                    select(Task).where(Task.parent_id == submitted.id, Task.kind == "role_reflect")
                )
            ]
            installed = [
                r.covered_seq for r in s.scalars(select(RoleSnapshot).where(RoleSnapshot.actor_id == keeper))
            ]
        if not success:
            # The final mandatory tail still cannot fit this smaller window.
            # Catch-up must stop without dropping it or spinning forever.
            assert task.status == "failed" and "超过容量" in task.error
            assert 2 <= len(before) <= 7 and len(installed) == len(before)
            return
        assert task.status == "completed", (task.status, task.error, before, installed)
        assert task.result["answer"] == "你好，我愿意继续听你说。"
        with db.session() as s:
            reflections = list(
                s.scalars(select(Task).where(Task.parent_id == submitted.id, Task.kind == "role_reflect"))
            )
            assert len(reflections) >= 2
            assert all(t.payload["urgent"] and t.status == "completed" for t in reflections)
            snapshots = list(
                s.scalars(
                    select(RoleSnapshot)
                    .where(RoleSnapshot.actor_id == keeper)
                    .order_by(RoleSnapshot.covered_seq)
                )
            )
            assert len(snapshots) == len(reflections)
            assert snapshots[0].covered_seq < snapshots[-1].covered_seq
    finally:
        worker.stop.set()
        for lane in lanes:
            lane.cancel()
        await asyncio.gather(*lanes, return_exceptions=True)


async def prepared_catchup(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake, serve_web=False)) as client:
        story, actors, player = launch(client)
    keeper = actors[0]
    for index in range(10):
        with db.session.begin() as s:
            append_record(
                s,
                story_id=story,
                kind="speech",
                text=f"旧记录{index}。",
                actor=s.get(Conversation, keeper),
                observer_ids=[keeper],
                dedupe=f"prefix-{index}",
            )
    root = runtime.submit(
        player, config.workspace_owner, "馆长，请听我说。", "catchup-root", addressees=[keeper]
    )
    root = runtime.claim("root")
    service = RoleMemoryService(db, config, runtime, fake)
    fake.replies["role_memory"] = {"summary": "旧记录已整理，具体依据仍在原始档案中。", "notes": []}
    job = service.schedule(keeper, config.workspace_owner, parent_id=root.id, force=True)
    claimed = runtime.claim("reflect", background=True)
    assert claimed.id == job.id
    result = await service.reflect(claimed)
    assert runtime.finish(claimed.id, claimed.lease_version, result)
    for index in range(4):
        with db.session.begin() as s:
            append_record(
                s,
                story_id=story,
                kind="speech",
                text=f"后续记录{index}。",
                actor=s.get(Conversation, keeper),
                observer_ids=[keeper],
                dedupe=f"remainder-{index}",
            )
    return service, root, job, keeper, actors[1]


@pytest.mark.parametrize(
    "mode",
    [
        "ordinary",
        "failed",
        "stale",
        "missing_snapshot",
        "privacy",
        "deleted_source",
        "wrong_cut",
        "other_actor",
        "root_done",
        "root_expired",
    ],
)
async def test_catchup_rejects_invalid_or_nonurgent_continuation(db, config, runtime, fake, mode):
    service, root, job, keeper, other = await prepared_catchup(db, config, runtime, fake)
    with db.session.begin() as s:
        previous = s.scalar(select(RoleSnapshot).where(RoleSnapshot.actor_id == keeper))
        last = s.get(Task, job.id)
        if mode == "failed":
            last.status = "failed"
        elif mode == "stale":
            last.result = {"stale": True}
        elif mode == "missing_snapshot":
            s.delete(previous)
        elif mode == "privacy":
            s.get(Conversation, keeper).privacy_version += 1
        elif mode == "deleted_source":
            s.get(SceneRecord, previous.source_ids[0]).redacted = True
        elif mode == "wrong_cut":
            last.result = {**last.result, "cut": previous.covered_seq + 1}
        elif mode == "root_done":
            s.get(Task, root.id).status = "completed"
        elif mode == "root_expired":
            s.get(Task, root.id).deadline = now() - timedelta(seconds=1)
    assert (
        service.schedule(
            other if mode == "other_actor" else keeper,
            config.workspace_owner,
            parent_id=root.id,
            force=mode != "ordinary",
        )
        is None
    )
    with db.session() as s:
        assert (
            len(list(s.scalars(select(Task).where(Task.parent_id == root.id, Task.kind == "role_reflect"))))
            == 1
        )


async def test_catchup_concurrent_schedule_reuses_one_prefix_and_parent_deadline(db, config, runtime, fake):
    service, root, job, keeper, _ = await prepared_catchup(db, config, runtime, fake)
    deadline = now() + timedelta(seconds=15)
    with db.session.begin() as s:
        s.get(Task, root.id).deadline = deadline

    def schedule():
        return service.schedule(keeper, config.workspace_owner, parent_id=root.id, force=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = await asyncio.gather(
            asyncio.wrap_future(pool.submit(schedule)), asyncio.wrap_future(pool.submit(schedule))
        )
    assert left.id == right.id and left.id != job.id
    with db.session() as s:
        last = s.get(Task, left.id)
        assert last.parent_id == root.id and last.payload["cut"] > job.payload["cut"]
        assert last.deadline <= deadline
        assert (
            len(list(s.scalars(select(Task).where(Task.parent_id == root.id, Task.kind == "role_reflect"))))
            == 2
        )
    # Actual Provider reservation still sums the root and both direct children.
    config.max_calls_per_task = 2
    provider = Provider(db, config, runtime)
    try:
        first = provider.reserve(job.id, "tts", "fixture", [], 0, price=(0, 0, 0))
        runtime.claim("next-reflect", background=True)
        second = provider.reserve(left.id, "role_memory", "fixture", [], 0, price=(0, 0, 0))
        with pytest.raises(BudgetExceeded):
            provider.reserve(root.id, "dialogue", "fixture", [], 0, price=(0, 0, 0))
        provider.settle(first, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0}, 0, "completed")
        provider.settle(second, {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0}, 0, "completed")
    finally:
        await provider.close()
