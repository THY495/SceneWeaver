"""A failed player turn must leave the same save usable without replaying its input."""

import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention, launch

from chatbot.api import create_app
from chatbot.game_api import create_game_app
from chatbot.providers import BudgetExceeded, TransientProviderError
from chatbot.runtime import StaleTask
from chatbot.storage import Message, SceneRecord, Task, now
from chatbot.worker import Worker

REJECTED = "这一段是尚未通过审核的候选。"
APPROVED = "你好，我愿意听你说。"


def snapshot(client, player):
    response = client.get(f"/api/role/players/{player}/snapshot?audio=false")
    assert response.status_code == 200, response.text
    return response.json()


def send(client, player, actor, key, text):
    response = client.post(
        f"/api/sessions/{player}/messages",
        json={"content": text, "client_message_id": key, "addressees": [actor]},
    )
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


@pytest.mark.parametrize("graph", [False, True])
@pytest.mark.parametrize("failure", ["review", "network", "budget", "deadline"])
async def test_failed_turn_recovers_in_same_save_after_app_recreation(db, config, fake, graph, failure):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake, serve_web=False)) as studio:
        _, actors, player = launch(studio)
    actor = actors[0]
    fake.replies["role_intent"] = intention(player)
    fake.replies["dialogue"] = {"text": REJECTED, "spoken_parts": [REJECTED], "claims": []}
    if failure == "review":
        fake.replies["actor_review"] = {"accepted": False, "issues": ["候选不得发表。"]}
    elif failure != "deadline":
        fake.replies["role_intent"] = (
            TransientProviderError("模型网络请求超时，请稍后重试。")
            if failure == "network"
            else BudgetExceeded("本次模拟预算耗尽。")
        )

    worker = Worker(db, config, fake)
    async with AsyncExitStack() as stack:
        if graph:
            await worker.setup_graph(stack)
        with TestClient(create_game_app(config, db, fake, player_id=player)) as game:
            assert snapshot(game, player)["interaction"]["allowed"]
            failed_id = send(game, player, actor, "failed-input", "馆长，请回应。")
            task = worker.runtime.claim("failure-window")
            assert task.id == failed_id
            assert snapshot(game, player)["interaction"]["responding"]
            if failure == "deadline":
                with db.session.begin() as s:
                    s.get(Task, failed_id).deadline = now() - timedelta(seconds=1)
                worker.runtime.expire()
            else:
                await worker.run_task(task)
            visible = game.get("/api/game/session").json()["tasks"]
            assert next(t for t in visible if t["id"] == failed_id)["status"] == "failed"
            assert all(set(t) <= {"id", "status", "epoch", "stop_reason"} for t in visible)
            recovered = snapshot(game, player)
            assert recovered["interaction"] == {"allowed": True, "reason": "", "responding": False}
            assert REJECTED not in str(recovered["events"])
            # Replaying the HTTP request ID retrieves the original failure; it must
            # not submit another input or silently restart a terminal generation.
            assert send(game, player, actor, "failed-input", "馆长，请回应。") == failed_id

        with db.session() as s:
            assert s.get(Task, failed_id).status == "failed"
            assert not list(
                s.scalars(select(Message).where(Message.task_id == failed_id, Message.role == "assistant"))
            )
            records = list(s.scalars(select(SceneRecord).where(SceneRecord.task_id == failed_id)))
            assert [e.text for e in records if e.kind == "speech"] == ["馆长，请回应。"]
        if failure == "review":
            assert len([c for c in fake.calls if c[0] == "actor_review"]) == 3

        # Recreate the application and worker while retaining the same persisted
        # run. This is not a separate OS-process or clean-machine restart test.
        fake.replies["role_intent"] = intention(player)
        fake.replies["dialogue"] = {"text": APPROVED, "spoken_parts": [APPROVED], "claims": []}
        fake.replies["actor_review"] = {"accepted": True, "issues": []}
        new_worker = Worker(db, config, fake)
        if graph:
            await new_worker.setup_graph(stack)
        with TestClient(create_game_app(config, db, fake, player_id=player)) as game:
            assert snapshot(game, player)["interaction"]["allowed"]
            next_id = send(game, player, actor, "next-input", "那我们继续聊。")
            assert next_id != failed_id
            next_task = new_worker.runtime.claim("recovered-window")
            assert next_task.id == next_id
            await new_worker.run_task(next_task)
            latest = snapshot(game, player)
            assert latest["interaction"]["allowed"] and not latest["interaction"]["responding"]
            assert REJECTED not in str(latest["events"])
            assert APPROVED in str(latest["events"])
        with db.session() as s:
            assert s.get(Task, failed_id).status == "failed"
            assert s.get(Task, next_id).status == "completed"
            replies = list(
                s.scalars(
                    select(SceneRecord).where(
                        SceneRecord.task_id == next_id,
                        SceneRecord.actor_id == actor,
                        SceneRecord.kind == "speech",
                    )
                )
            )
            assert len(replies) == 1 and replies[0].text == APPROVED
            assert replies[0].parent_id == s.get(Task, next_id).payload["event_id"]


@pytest.mark.parametrize("graph", [False, True])
async def test_lane_expires_inflight_model_then_accepts_new_input(db, config, fake, graph):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake, serve_web=False)) as studio:
        _, actors, player = launch(studio)
    fake.replies["role_intent"] = intention(player)
    fake.replies["dialogue"] = {"text": APPROVED, "spoken_parts": [APPROVED], "claims": []}
    fake.replies["actor_review"] = {"accepted": True, "issues": []}
    entered, cancelled = asyncio.Event(), asyncio.Event()
    original_chat = fake.chat
    blocked_calls = 0

    async def gated_chat(task_id, role, messages, **kwargs):
        nonlocal blocked_calls
        if role == "role_intent" and not entered.is_set():
            blocked_calls += 1
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return await original_chat(task_id, role, messages, **kwargs)

    fake.chat = gated_chat
    worker = Worker(db, config, fake)

    async def wait_status(task_id, status):
        for _ in range(100):
            with db.session() as s:
                if s.get(Task, task_id).status == status:
                    return
            await asyncio.sleep(0.05)
        pytest.fail(f"Task did not reach {status}")

    async with AsyncExitStack() as stack:
        if graph:
            await worker.setup_graph(stack)
        with TestClient(create_game_app(config, db, fake, player_id=player)) as game:
            first = send(game, player, actors[0], "inflight-deadline", "请听我说。")
            lane = asyncio.create_task(worker.lane(0, False))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                assert snapshot(game, player)["interaction"]["responding"]
                with db.session.begin() as s:
                    task = s.get(Task, first)
                    assert task.status == "running"
                    task.deadline = now() + timedelta(seconds=0.2)
                # Actual lane detects deadline, cancels/drains the model coroutine,
                # and expires the task itself. No direct expire or finish call.
                await wait_status(first, "failed")
                assert cancelled.is_set() and blocked_calls == 1
                assert not lane.done()
                with pytest.raises(StaleTask):
                    worker.runtime.check(first, task.lease_version)
                assert not worker.runtime.finish(first, task.lease_version, {"answer": REJECTED})
                assert snapshot(game, player)["interaction"]["allowed"]
                with db.session() as s:
                    assert not list(
                        s.scalars(
                            select(SceneRecord).where(
                                SceneRecord.task_id == first,
                                SceneRecord.actor_id.in_(actors),
                                SceneRecord.kind == "speech",
                            )
                        )
                    )
                second = send(game, player, actors[0], "after-deadline", "现在继续吧。")
                await wait_status(second, "completed")
                projected = snapshot(game, player)
                assert projected["interaction"]["allowed"]
                assert APPROVED in str(projected["events"])
                assert not lane.done()
            finally:
                worker.stop.set()
                lane.cancel()
                await asyncio.gather(lane, return_exceptions=True)
