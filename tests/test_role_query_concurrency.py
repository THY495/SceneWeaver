"""Concurrent query preparation must not pool private memories or leak requests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from support.story import launch

from chatbot.api import create_app
from chatbot.providers import BudgetExceeded, Embeddings, Provider, ProviderError
from chatbot.role_memory import RoleMemoryService
from chatbot.runtime import StaleTask
from chatbot.storage import Conversation, Operation, RoleMemory, Task, Usage


@pytest.fixture
def query_case(db, config, runtime, fake):
    config.product = "roleplay"
    config.embedding_provider = "api"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "回忆我之前的说法。", "concurrent-query")
    task = runtime.claim("query-test")
    with db.session.begin() as s:
        for index, actor in enumerate(actors[:2]):
            s.add(
                RoleMemory(
                    story_id=story,
                    actor_id=actor,
                    kind="belief",
                    content=f"角色{index}独有的私人记忆",
                    source_ids=[],
                    embedding=[1.0] * 1024,
                    embedding_model=config.embedding_model,
                )
            )
    return SimpleNamespace(
        db=db,
        config=config,
        runtime=runtime,
        task=task,
        actors=actors,
        player=player,
        story=story,
        service=RoleMemoryService(db, config, runtime, fake),
    )


async def launch_pair(case, monkeypatch, *, failure=None):
    started, release = asyncio.Event(), asyncio.Event()

    async def encode(*args, **kwargs):
        started.set()
        await release.wait()
        if failure:
            raise failure
        return [[2.0] * 1024]

    request = AsyncMock(side_effect=encode)
    monkeypatch.setattr(Embeddings, "encode", request)
    tasks = [asyncio.create_task(case.service.prepare_recall(case.task, actor)) for actor in case.actors[:2]]
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.sleep(0)
    return tasks, release, request


async def test_parallel_first_recall_only_encodes_shared_query_once(query_case, monkeypatch):
    case = query_case
    tasks, release, request = await launch_pair(case, monkeypatch)
    release.set()
    await asyncio.gather(*tasks)
    assert request.await_count == 1
    with case.db.session() as s:
        rows = list(s.scalars(select(Operation).where(Operation.id == f"{case.task.id}:role-query")))
        assert len(rows) == 1 and rows[0].payload["vector"][0] == 2.0
    assert not case.service._query_flights


async def test_cancel_one_waiter_preserves_other_recall(query_case, monkeypatch):
    case = query_case
    tasks, release, request = await launch_pair(case, monkeypatch)
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    assert not tasks[1].done()
    release.set()
    await tasks[1]
    assert request.await_count == 1 and not case.service._query_flights
    with case.db.session() as s:
        assert s.get(Operation, f"{case.task.id}:role-query").payload.get("vector")


async def test_last_cancelled_waiter_cancels_request_and_cleans_flight(query_case, monkeypatch):
    case = query_case
    tasks, _, request = await launch_pair(case, monkeypatch)
    future = next(iter(case.service._query_flights.values())).future
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert request.await_count == 1 and future.cancelled() and not case.service._query_flights
    with case.db.session() as s:
        assert not s.get(Operation, f"{case.task.id}:role-query")


async def test_shared_vector_does_not_share_private_memory_or_revive_deleted_note(query_case, monkeypatch):
    import json

    case = query_case
    tasks, release, _ = await launch_pair(case, monkeypatch)
    release.set()
    await asyncio.gather(*tasks)
    for index, actor in enumerate(case.actors[:2]):
        messages, _ = case.service.context(case.task, actor)
        text = json.dumps(messages, ensure_ascii=False)
        assert f"角色{index}独有的私人记忆" in text
        assert f"角色{1 - index}独有的私人记忆" not in text
    with case.db.session.begin() as s:
        memory = s.scalar(
            select(RoleMemory).where(RoleMemory.actor_id == case.actors[0], RoleMemory.kind == "belief")
        )
        memory.status = "deleted"
    messages, _ = case.service.context(case.task, case.actors[0])
    assert "角色0独有的私人记忆" not in json.dumps(messages, ensure_ascii=False)


async def test_provider_failure_is_shared_fallback_and_does_not_retry_for_each_actor(query_case, monkeypatch):
    case = query_case
    tasks, release, request = await launch_pair(case, monkeypatch, failure=ProviderError("request failed"))
    release.set()
    await asyncio.gather(*tasks)
    await case.service.prepare_recall(case.task, case.actors[1])
    assert request.await_count == 1 and not case.service._query_flights
    with case.db.session() as s:
        assert s.get(Operation, f"{case.task.id}:role-query").payload == {"unavailable": True}


async def test_budget_limit_propagates_to_all_waiters_and_next_task_can_recall(query_case, monkeypatch):
    case = query_case
    tasks, release, request = await launch_pair(case, monkeypatch, failure=BudgetExceeded("budget limit"))
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, BudgetExceeded) for result in results)
    assert request.await_count == 1 and not case.service._query_flights
    with case.db.session() as s:
        assert not s.get(Operation, f"{case.task.id}:role-query")
    case.runtime.submit(case.player, case.config.workspace_owner, "新的检索请求。", "next-query")
    later = case.runtime.claim("next-query-test")
    request = AsyncMock(return_value=[[3.0] * 1024])
    monkeypatch.setattr(Embeddings, "encode", request)
    await case.service.prepare_recall(later, case.actors[0])
    assert request.await_count == 1 and not case.service._query_flights
    with case.db.session() as s:
        assert s.get(Operation, f"{later.id}:role-query").payload["vector"][0] == 3.0


@pytest.mark.parametrize("change", ["epoch", "lease", "cancel"])
async def test_stopped_turn_does_not_store_query_and_all_waiters_fail(query_case, monkeypatch, change):
    case = query_case
    tasks, release, _ = await launch_pair(case, monkeypatch)
    with case.db.session.begin() as s:
        if change == "epoch":
            s.get(Conversation, case.player).epoch += 1
        elif change == "lease":
            s.get(Task, case.task.id).lease_version += 1
        else:
            s.get(Task, case.task.id).status = "cancelled"
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, StaleTask) for result in results)
    assert not case.service._query_flights
    with case.db.session() as s:
        assert not s.get(Operation, f"{case.task.id}:role-query")


async def test_restart_uses_completed_cache_and_no_index_does_not_start_request(query_case, monkeypatch):
    case = query_case
    request = AsyncMock(return_value=[[2.0] * 1024])
    monkeypatch.setattr(Embeddings, "encode", request)
    await case.service.prepare_recall(case.task, case.actors[2])
    assert not request.await_count
    await case.service.prepare_recall(case.task, case.actors[0])
    restarted = RoleMemoryService(case.db, case.config, case.runtime, case.service.provider)
    await restarted.prepare_recall(case.task, case.actors[1])
    assert request.await_count == 1 and not restarted._query_flights


async def test_other_story_actor_cannot_join_shared_query(query_case, monkeypatch, fake):
    case = query_case
    with TestClient(create_app(case.config, case.db, fake)) as client:
        _, foreign_actors, _ = launch(client)
    tasks, release, request = await launch_pair(case, monkeypatch)
    with pytest.raises(ProviderError, match="范围"):
        await case.service.prepare_recall(case.task, foreign_actors[0])
    release.set()
    await asyncio.gather(*tasks)
    assert request.await_count == 1 and not case.service._query_flights


async def test_last_cancellation_settles_real_embedding_reservation_conservatively(query_case, monkeypatch):
    case = query_case
    case.config.embedding_api_key = SecretStr("local-test-credential")
    provider = Provider(case.db, case.config, case.runtime)
    service = RoleMemoryService(case.db, case.config, case.runtime, provider)
    started = asyncio.Event()

    async def transport(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(Embeddings, "_request", transport)
    tasks = [asyncio.create_task(service.prepare_recall(case.task, actor)) for actor in case.actors[:2]]
    try:
        await asyncio.wait_for(started.wait(), 2)
        with case.db.session() as s:
            rows = list(
                s.scalars(select(Usage).where(Usage.task_id == case.task.id, Usage.role == "embedding"))
            )
            assert len(rows) == 1
            reserved = rows[0].reserved
            assert reserved > 0
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        with case.db.session() as s:
            usage = s.get(Usage, rows[0].id)
            assert usage.reserved == 0 and usage.cost == reserved and usage.status == "estimated_failed"
            assert not s.get(Operation, f"{case.task.id}:role-query")
        assert not service._query_flights
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await provider.close()


async def test_all_waiters_cancelled_can_restart_same_still_valid_task(query_case, monkeypatch):
    case = query_case
    tasks, _, _ = await launch_pair(case, monkeypatch)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    request = AsyncMock(return_value=[[4.0] * 1024])
    monkeypatch.setattr(Embeddings, "encode", request)
    await asyncio.wait_for(case.service.prepare_recall(case.task, case.actors[0]), 2)
    assert request.await_count == 1 and not case.service._query_flights
    with case.db.session() as s:
        assert s.get(Operation, f"{case.task.id}:role-query").payload["vector"][0] == 4.0
