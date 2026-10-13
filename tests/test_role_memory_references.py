"""Invalid merge references must not silently become new subjective memories."""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from support.story import launch

from chatbot.api import create_app
from chatbot.observations import append_record, visible_records
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, RoleMemory, RoleSnapshot, Story, uid


def seed(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = launch(client)
    actor = actors[0]
    with db.session.begin() as session:
        records = [append_record(session, story_id=story, kind="speech",
            actor=session.get(Conversation, actor), observer_ids=[actor],
            text="我认为灯塔信号尚需核对。", dedupe=f"reference-{index}") for index in range(5)]
        source = records[0].id
    return story, actors, source


def reply(source, same_as):
    return {"summary": "馆长认为灯塔信号尚需核对，尚未确认。", "notes": [
        {"kind": "belief", "content": "灯塔信号尚未核实。", "source_id": source,
         "quote": "我认为灯塔信号尚需核对。", "same_as": same_as},
        {"kind": "impression", "content": "对当前信号仍有疑虑。", "source_id": source,
         "quote": "我认为灯塔信号尚需核对。", "same_as": None},
    ]}


async def run(service, runtime, actor, owner):
    scheduled = service.schedule(actor, owner, force=True)
    task = runtime.claim("reference-check", background=True)
    assert task.id == scheduled.id
    result = await service.reflect(task)
    assert runtime.finish(task.id, task.lease_version, result)
    return result


@pytest.mark.parametrize("target", ["event", "foreign", "unknown", "empty", "archived"])
async def test_invalid_same_as_skips_only_derived_note(db, config, runtime, fake, target):
    story, actors, source = seed(db, config, fake)
    reference = {"event": source, "unknown": uid(), "empty": ""}.get(target, uid())
    if target in {"foreign", "archived"}:
        with db.session.begin() as session:
            session.add(RoleMemory(id=reference, story_id=story,
                actor_id=actors[1] if target == "foreign" else actors[0], kind="belief",
                content="旧判断", source_ids=[source], status="archived" if target == "archived" else "active"))
    fake.replies["role_memory"] = reply(source, reference)
    result = await run(RoleMemoryService(db, config, runtime, fake), runtime, actors[0], config.workspace_owner)
    assert result["notes"] == 1 and result["note_rejections"] == {"unknown_same_as": 1}
    with db.session() as session:
        rows = list(session.scalars(select(RoleMemory).where(RoleMemory.actor_id == actors[0],
                                                             RoleMemory.kind != "episode",
                                                             RoleMemory.status == "active")))
        assert [(row.kind, row.content) for row in rows] == [("impression", "对当前信号仍有疑虑。")]
        snapshot = session.scalar(select(RoleSnapshot).where(RoleSnapshot.actor_id == actors[0]))
        assert snapshot.summary["summary"] == reply(source, reference)["summary"]
        assert source in snapshot.source_ids and source in {e.id for e in visible_records(session, actors[0])}
    assert len(fake.calls) == 1  # No regenerating the summary to repair an optional note.


@pytest.mark.parametrize("change,reason", [
    ("status", "ineligible_same_as"), ("kind", "ineligible_same_as"),
    ("version", "changed_same_as"), ("content", "changed_same_as"),
])
async def test_changed_target_cannot_merge_or_fall_back_to_insert(db, config, runtime, fake, change, reason):
    story, actors, source = seed(db, config, fake)
    mid = uid()
    with db.session.begin() as session:
        session.add(RoleMemory(id=mid, story_id=story, actor_id=actors[0], kind="belief",
                               content="灯塔信号尚未核实。", source_ids=[source]))

    def during_generation(messages):
        with db.session.begin() as session:
            target = session.get(RoleMemory, mid)
            if change == "status":
                target.status = "archived"
            elif change == "kind":
                target.kind = "goal"
            elif change == "version":
                target.version += 1
            else:
                target.content = "判断已经改变。"
        return reply(source, mid)

    fake.replies["role_memory"] = during_generation
    result = await run(RoleMemoryService(db, config, runtime, fake), runtime, actors[0], config.workspace_owner)
    assert result["notes"] == 1 and result["note_rejections"] == {reason: 1}
    with db.session() as session:
        target = session.get(RoleMemory, mid)
        assert target.replaces_id is None
        rows = list(session.scalars(select(RoleMemory).where(RoleMemory.actor_id == actors[0],
                                                             RoleMemory.kind != "episode")))
        assert len(rows) == 2 and sum(row.kind == "impression" for row in rows) == 1
    assert len(fake.calls) == 1


async def test_explicit_null_still_creates_and_valid_reference_still_merges(db, config, runtime, fake):
    story, actors, source = seed(db, config, fake)
    mid = uid()
    with db.session.begin() as session:
        session.add(RoleMemory(id=mid, story_id=story, actor_id=actors[0], kind="belief",
                               content="灯塔信号尚未核实。", source_ids=[source]))
    fake.replies["role_memory"] = reply(source, mid)
    result = await run(RoleMemoryService(db, config, runtime, fake), runtime, actors[0], config.workspace_owner)
    assert result["notes"] == 1 and result["note_rejections"] == {}
    with db.session() as session:
        target = session.get(RoleMemory, mid)
        assert target.id == mid and target.content == "灯塔信号尚未核实。" and target.version == 2
        assert session.get(RoleMemory, target.replaces_id).status == "archived"
        assert len(list(session.scalars(select(RoleMemory).where(RoleMemory.actor_id == actors[0],
                                                                 RoleMemory.kind != "episode",
                                                                 RoleMemory.status == "active")))) == 2


async def test_two_valid_references_do_not_mistake_own_merge_for_external_change(db, config, runtime, fake):
    story, actors, source = seed(db, config, fake)
    mid = uid()
    with db.session.begin() as session:
        session.add(RoleMemory(id=mid, story_id=story, actor_id=actors[0], kind="belief",
                               content="灯塔信号尚未核实。", source_ids=[source]))
        second = visible_records(session, actors[0])[1].id
    response = reply(source, mid)
    response["notes"] = [response["notes"][0], {**response["notes"][0], "source_id": second}]
    fake.replies["role_memory"] = response
    result = await run(RoleMemoryService(db, config, runtime, fake), runtime, actors[0], config.workspace_owner)
    assert result["notes"] == 0 and result["note_rejections"] == {}
    with db.session() as session:
        target = session.get(RoleMemory, mid)
        assert target.version == 3 and set(target.source_ids) == {source, second}


def test_memory_patch_waits_before_target_lock_and_rechecks_committed_version(db, config, runtime, fake, monkeypatch):
    story, actors, source = seed(db, config, fake)
    mid = uid()
    with db.session.begin() as session:
        session.add(RoleMemory(id=mid, story_id=story, actor_id=actors[0], kind="belief",
                               content="灯塔信号尚未核实。", source_ids=[source]))
    fake.replies["role_memory"] = reply(source, mid)
    service = RoleMemoryService(db, config, runtime, fake)
    scheduled = service.schedule(actors[0], config.workspace_owner, force=True)
    task = runtime.claim("install-race", background=True)
    assert task.id == scheduled.id
    entered, release, patch_entered = threading.Event(), threading.Event(), threading.Event()
    identities, pids = {}, {}
    session_type = db.session.class_
    original_get, original_scalar = session_type.get, session_type.scalar

    def lock_timeout(connection):
        connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")

    def held_get(session, entity, ident, *args, **kwargs):
        value = original_get(session, entity, ident, *args, **kwargs)
        if entity is RoleMemory and ident == mid and threading.get_ident() == identities.get("reflect"):
            pids["reflect"] = session.connection().scalar(text("SELECT pg_backend_pid()"))
            entered.set()
            assert release.wait(8), "Bounded installation coordination expired"
        return value

    def observe_patch_story_lock(session, statement, *args, **kwargs):
        # The request now waits at Story, before it can acquire either the actor
        # or target memory. Observe this real request's backend, not elapsed time.
        entities = [d.get("entity") for d in getattr(statement, "column_descriptions", [])]
        if (Story in entities and getattr(statement, "_for_update_arg", None) is not None
                and threading.get_ident() != identities.get("reflect")):
            pids["patch"] = session.connection().scalar(text("SELECT pg_backend_pid()"))
            patch_entered.set()
        return original_scalar(session, statement, *args, **kwargs)

    monkeypatch.setattr(session_type, "get", held_get)
    monkeypatch.setattr(session_type, "scalar", observe_patch_story_lock)
    event.listen(db.engine, "begin", lock_timeout)

    def install():
        identities["reflect"] = threading.get_ident()
        value = asyncio.run(service.reflect(task))
        assert runtime.finish(task.id, task.lease_version, value)
        return value

    try:
        with TestClient(create_app(config, db, fake)) as client, ThreadPoolExecutor(max_workers=2) as pool:
            reflection = pool.submit(install)
            try:
                assert entered.wait(5)
                patch = pool.submit(client.patch, f"/api/role/actors/{actors[0]}/memories/{mid}",
                                    json={"expected_version": 1, "status": "archived"})
                assert patch_entered.wait(5)
                deadline = time.monotonic() + 2
                while True:
                    with db.engine.connect() as connection:
                        blocking = connection.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": pids["patch"]})
                    if pids["reflect"] in blocking:
                        break
                    assert time.monotonic() < deadline, "PATCH did not actually wait on reflection"
                    time.sleep(0.01)
                # A waiting PATCH must not already hold the target memory lock.
                with db.engine.begin() as connection:
                    assert connection.execute(select(RoleMemory.id).where(RoleMemory.id == mid)
                                               .with_for_update(nowait=True)).scalar_one() == mid
            finally:
                release.set()
            assert reflection.result(timeout=8)["note_rejections"] == {}
            response = patch.result(timeout=8)
            assert response.status_code == 409  # Reflect committed v2; old v1 PATCH cannot overwrite it.
        with db.session() as session:
            target = session.get(RoleMemory, mid)
            assert target.version == 2 and target.status == "active"
            assert session.get(RoleMemory, target.replaces_id).status == "archived"
    finally:
        release.set()
        event.remove(db.engine, "begin", lock_timeout)
