import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.agreements import command, consent
from support.agreements import setup as agreement_setup
from support.story import intention, launch, persona

from chatbot.action_attempts import reserve
from chatbot.agreements import proposals
from chatbot.api import create_app
from chatbot.runtime import StaleTask
from chatbot.scene_state import objects
from chatbot.storage import ActionAttempt, Conversation, SceneRecord, Task, now
from chatbot.worker import Worker


class GatedProvider:
    def __init__(self, base, predicate=None):
        self.base = base
        self.predicate = predicate or (lambda role, messages: role == "role_intent")
        self.entered, self.release, self.cancelled = (asyncio.Event() for _ in range(3))

    async def chat(self, task_id, role, messages, **kwargs):
        if self.predicate(role, messages) and not self.release.is_set():
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return await self.base.chat(task_id, role, messages, **kwargs)

    async def close(self):
        await self.base.close()


def setup(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    task = runtime.submit(
        player, config.workspace_owner, "馆长，请说说你的看法。", "lifecycle", addressees=[actors[0]]
    )
    return actors, player, task


def speeches(db, task_id):
    with db.session() as s:
        return list(
            s.scalars(
                select(SceneRecord)
                .where(
                    SceneRecord.task_id == task_id,
                    SceneRecord.kind == "speech",
                    SceneRecord.dedupe != f"input:{task_id}",
                )
                .order_by(SceneRecord.seq)
            )
        )


async def test_worker_coroutine_cancellation_propagates_and_leaves_recoverable_lease(
    db, config, runtime, fake
):
    actors, player, submitted = setup(db, config, runtime, fake)
    task = runtime.claim("old")
    gate = GatedProvider(fake)
    worker = Worker(db, config, gate)
    executing = asyncio.create_task(worker.run_task(task))
    try:
        await asyncio.wait_for(gate.entered.wait(), 3)
        executing.cancel()
        result = await asyncio.wait_for(asyncio.gather(executing, return_exceptions=True), 3)
        assert gate.cancelled.is_set()
        assert isinstance(result[0], asyncio.CancelledError)
        with db.session() as s:
            current = s.get(Task, submitted.id)
            assert current.status == "running" and current.lease_version == task.lease_version
            assert s.get(Conversation, player).epoch == task.epoch
            assert not [e for e in speeches(db, task.id) if e.actor_id in actors]
    finally:
        gate.release.set()
        executing.cancel()
        await asyncio.gather(executing, return_exceptions=True)


async def test_cancelling_active_worker_lane_stops_the_lane(db, config, runtime, fake):
    setup(db, config, runtime, fake)
    gate = GatedProvider(fake)
    worker = Worker(db, config, gate)
    lane = asyncio.create_task(worker.lane(0, background=False))
    try:
        await asyncio.wait_for(gate.entered.wait(), 3)
        lane.cancel()
        done, _ = await asyncio.wait({lane}, timeout=1)
        assert lane in done, "Active run_task swallowed lane cancellation"
        assert lane.cancelled() and gate.cancelled.is_set()
    finally:
        worker.stop.set()
        gate.release.set()
        lane.cancel()
        await asyncio.wait_for(asyncio.gather(lane, return_exceptions=True), 3)


async def test_old_graph_entry_cannot_borrow_reclaimed_lease(db, config, runtime, fake, monkeypatch):
    actors, _, submitted = setup(db, config, runtime, fake)
    task = runtime.claim("old-graph")
    worker = Worker(db, config, fake)
    entered, release = asyncio.Event(), asyncio.Event()
    async with AsyncExitStack() as stack:
        await worker.setup_graph(stack)
        get_state = worker.graph.aget_state

        async def delayed_entry(*args, **kwargs):
            state = await get_state(*args, **kwargs)
            entered.set()
            await release.wait()
            return state

        monkeypatch.setattr(worker.graph, "aget_state", delayed_entry)
        executing = asyncio.create_task(worker.run_task(task))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            with db.session.begin() as s:
                s.get(Task, task.id).lease_until = now() - timedelta(seconds=1)
            fresh = runtime.claim("new-graph")
            assert fresh.id == submitted.id and fresh.lease_version > task.lease_version
            release.set()
            await asyncio.wait_for(executing, 5)
            assert not fake.calls, "Old graph executed the freshly reclaimed lease"
            assert not [e for e in speeches(db, task.id) if e.actor_id in actors]
            with db.session() as s:
                current = s.get(Task, task.id)
                assert current.status == "running" and current.lease_version == fresh.lease_version
        finally:
            release.set()
            executing.cancel()
            await asyncio.gather(executing, return_exceptions=True)


async def test_player_cancel_fences_pending_role_reply_without_reclaim(db, config, runtime, fake):
    actors, player, submitted = setup(db, config, runtime, fake)
    task = runtime.claim("player-cancel")
    gate = GatedProvider(fake)
    worker = Worker(db, config, gate)
    executing = asyncio.create_task(worker.run_task(task))
    try:
        await asyncio.wait_for(gate.entered.wait(), 3)
        runtime.cancel(task.id, config.workspace_owner)
        runtime.cancel(task.id, config.workspace_owner)
        await asyncio.wait_for(executing, 3)
        assert gate.cancelled.is_set()
        with db.session() as s:
            assert s.get(Task, submitted.id).status == "cancelled"
            assert s.get(Conversation, player).epoch == task.epoch + 1
        assert runtime.claim("cannot-reclaim-cancelled") is None
        with pytest.raises(StaleTask):
            runtime.publish(task.id, task.lease_version, "role_reply", {"text": "迟到的台词"})
        assert not runtime.finish(task.id, task.lease_version, {"answer": "迟到"})
        assert not [e for e in speeches(db, task.id) if e.actor_id in actors]
    finally:
        gate.release.set()
        executing.cancel()
        await asyncio.gather(executing, return_exceptions=True)


@pytest.mark.parametrize("graph", [False, True])
async def test_reclaimed_worker_resumes_committed_role_prefix_once(db, config, runtime, fake, graph):
    actors, player, _ = setup(db, config, runtime, fake)
    keeper, listener = actors[:2]
    fake.replies["role_intent"] = lambda messages: (
        intention(listener, expects_reply=True, wait_for_player=False)
        if persona(messages) == "馆长"
        else intention(player)
    )
    fake.replies["dialogue"] = lambda messages: (
        "修复师，你怎么看？" if persona(messages) == "馆长" else "我先保留疑问。"
    )
    task = runtime.claim("prefix-old")
    gate = GatedProvider(fake, lambda role, messages: role == "dialogue" and persona(messages) == "修复师")
    old = Worker(db, config, gate)
    fresh_worker = Worker(db, config, fake)
    async with AsyncExitStack() as stack:
        if graph:
            await old.setup_graph(stack)
            await fresh_worker.setup_graph(stack)
        executing = asyncio.create_task(old.run_task(task))
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            prefix = [e for e in speeches(db, task.id) if e.actor_id in actors]
            assert len(prefix) == 1 and prefix[0].actor_id == keeper
            prefix_id = prefix[0].id
            executing.cancel()
            result = await asyncio.wait_for(asyncio.gather(executing, return_exceptions=True), 3)
            assert isinstance(result[0], asyncio.CancelledError) and gate.cancelled.is_set()
            with db.session.begin() as s:
                s.get(Task, task.id).lease_until = now() - timedelta(seconds=1)
            fresh = fresh_worker.runtime.claim("prefix-new")
            assert fresh.id == task.id and fresh.lease_version > task.lease_version
            assert not runtime.heartbeat(task.id, task.lease_version)
            assert not runtime.finish(task.id, task.lease_version, {"answer": "旧租约"})
            with db.session() as s:
                revision = s.get(Conversation, listener).revision
            with pytest.raises(StaleTask):
                runtime.commit_role_segment(
                    task, listener, "旧稿", step=1, revision=revision, intent=intention(player)
                )
            await asyncio.wait_for(fresh_worker.run_task(fresh), 5)
            records = [e for e in speeches(db, task.id) if e.actor_id in actors]
            assert [e.actor_id for e in records] == [keeper, listener]
            assert records[0].id == prefix_id
            assert [e.text for e in records] == ["修复师，你怎么看？", "我先保留疑问。"]
            assert (
                sum(role == "dialogue" and persona(messages) == "馆长" for role, messages, _ in fake.calls)
                == 1
            )
            with db.session() as s:
                completed = s.get(Task, task.id)
                assert completed.status == "completed" and completed.attempt == 2
                assert s.get(Conversation, player).epoch == task.epoch
            if graph:
                state = await fresh_worker.graph.aget_state({"configurable": {"thread_id": task.id}})
                assert state.values == {"task_id": task.id, "phase": "done"}
        finally:
            gate.release.set()
            executing.cancel()
            await asyncio.gather(executing, return_exceptions=True)


async def test_worker_reclaim_keeps_committed_item_transfer_atomic(db, config, runtime, fake):
    story, actors, player, _, data = agreement_setup(db, config, fake)
    with db.session.begin() as s:
        for actor_id in actors[:2]:
            consent(s, s.get(Conversation, actor_id), data)
    submitted = runtime.submit(player, config.workspace_owner, "请按已接受的提案转交铜盒。", "item-lifecycle")
    task = runtime.claim("item-old")
    cmd = command(data)
    committed, hold = asyncio.Event(), asyncio.Event()
    invocations = []
    event_ids = []

    class ControlledActionExecutor:
        # Exercise Worker + real reservation/atomic commit without adding model randomness.
        async def execute(self, active):
            invocations.append(active.lease_version)
            reserve(db, active, actors[0], 0, cmd)
            with db.session() as s:
                revision = s.get(Conversation, actors[0]).revision
            event_ids.append(
                runtime.commit_role_segment(
                    active,
                    actors[0],
                    "",
                    step=0,
                    revision=revision,
                    intent={"act": "object_action", "object_action": cmd},
                )
            )
            if len(invocations) == 1:
                committed.set()
                await hold.wait()
            return {"incremental": True, "answer": "铜盒转交已完成。"}

    old, new = Worker(db, config, fake), Worker(db, config, fake)
    old.agents = new.agents = ControlledActionExecutor()
    executing = asyncio.create_task(old.run_task(task))
    try:
        await asyncio.wait_for(committed.wait(), 3)
        with db.session() as s:
            first = next(item for item in objects(s, story) if item["id"] == "box")
            assert first["holder_id"] == actors[1] and first["version"] == 2
        executing.cancel()
        result = await asyncio.wait_for(asyncio.gather(executing, return_exceptions=True), 3)
        assert isinstance(result[0], asyncio.CancelledError)
        with db.session.begin() as s:
            s.get(Task, task.id).lease_until = now() - timedelta(seconds=1)
        fresh = new.runtime.claim("item-new")
        assert fresh.id == submitted.id and fresh.lease_version > task.lease_version
        with pytest.raises(StaleTask):
            reserve(db, task, actors[0], 1, cmd)
        await asyncio.wait_for(new.run_task(fresh), 3)
        assert event_ids[0] == event_ids[1] and invocations == [task.lease_version, fresh.lease_version]
        with db.session() as s:
            final = next(item for item in objects(s, story) if item["id"] == "box")
            assert final == first
            committed_records = list(
                s.scalars(select(SceneRecord).where(SceneRecord.dedupe == f"task:{task.id}:0"))
            )
            assert len(committed_records) == 1
            attempt = s.get(ActionAttempt, f"task:{task.id}:0")
            assert attempt.status == "completed" and attempt.result_event_id == event_ids[0]
            assert proposals(s, story, actors[0])[0]["completed"][data["items"][0]["id"]] == event_ids[0]
            assert s.get(Task, task.id).status == "completed"
        assert not fake.calls
    finally:
        hold.set()
        executing.cancel()
        await asyncio.gather(executing, return_exceptions=True)
