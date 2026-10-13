"""A reflection installer must fence its original lease through commit."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from support.story import launch

from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, Operation, RoleMemory, RoleSnapshot, Task, now


def reflection_case(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = launch(client)
    actor = actors[0]
    quote = "我答应明天核对仓库记录。"
    for index in range(4):
        with db.session.begin() as session:
            record = append_record(session, story_id=story, kind="speech",
                                   actor=session.get(Conversation, actor),
                                   text=quote if index == 0 else "还有记录需要核对。",
                                   channel="private", observer_ids=[actor],
                                   dedupe=f"reflection-fence-{index}")
            if index == 0:
                source = record.id
    fake.replies["role_memory"] = {
        "summary": "我答应明天核对仓库记录。", "notes": [{
            "kind": "commitment", "content": "明天核对仓库记录。",
            "source_id": source, "quote": quote, "same_as": None,
        }],
    }
    service = RoleMemoryService(db, config, runtime, fake)
    scheduled = service.schedule(actor, config.workspace_owner, force=True)
    original = runtime.claim("reflection-old", background=True)
    assert original.id == scheduled.id
    return actor, service, original


def test_reflection_installation_excludes_concurrent_lease_takeover(db, config, runtime, fake):
    actor, service, original = reflection_case(db, config, runtime, fake)
    checked, release = threading.Event(), threading.Event()

    def hold_after_lease_check(conn, cursor, statement, parameters, context, many):
        # The deduplication read follows the installer's lease/visibility checks.
        # Only hold that installation read, never setup or other connections.
        if (statement.lstrip().startswith("SELECT operations.")
            and f"{original.id}:reflected" in parameters.values()):
            checked.set()
            if not release.wait(5):
                raise TimeoutError("Reflection installation test gate timed out")

    event.listen(db.engine, "after_cursor_execute", hold_after_lease_check)
    fenced = False
    replacement = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            installing = pool.submit(lambda: asyncio.run(service.reflect(original)))
            try:
                assert checked.wait(5), "Reflection never reached the installation gate"
                try:
                    with db.session.begin() as session:
                        row = session.scalar(select(Task).where(Task.id == original.id)
                                             .with_for_update(nowait=True))
                        row.lease_until = now() - timedelta(seconds=1)
                except OperationalError as exc:
                    if getattr(exc.orig, "sqlstate", None) != "55P03":
                        raise
                    fenced = True
                if not fenced:
                    replacement = runtime.claim("reflection-new", background=True)
                    assert replacement.id == original.id
                    assert replacement.lease_version == original.lease_version + 1
            finally:
                release.set()
            outcome = installing.result(timeout=5)
        with db.session() as session:
            installed = {
                "snapshot": session.scalar(select(RoleSnapshot.id).where(RoleSnapshot.actor_id == actor)) is not None,
                "commitment": session.scalar(select(RoleMemory.id).where(
                    RoleMemory.actor_id == actor, RoleMemory.kind == "commitment")) is not None,
                "operation": session.get(Operation, f"{original.id}:reflected") is not None,
            }
        assert fenced, {"replacement_claimed": replacement is not None, "installed": installed}
        assert outcome["notes"] == 1 and all(installed.values())
        assert runtime.finish(original.id, original.lease_version, outcome)
    finally:
        release.set()
        event.remove(db.engine, "after_cursor_execute", hold_after_lease_check)


@pytest.mark.parametrize("change", ["reclaimed", "deadline"])
def test_changed_task_before_install_lock_cannot_publish_memory(db, config, runtime, fake, change):
    actor, service, original = reflection_case(db, config, runtime, fake)
    before_lock, release = threading.Event(), threading.Event()
    installation_thread = []

    def hold_before_task_lock(conn, cursor, statement, parameters, context, many):
        if (installation_thread and threading.get_ident() == installation_thread[0]
            and statement.lstrip().startswith("SELECT tasks.id")
            and original.id in parameters.values()):
            before_lock.set()
            if not release.wait(5):
                raise TimeoutError("Reflection task-lock test gate timed out")

    def install():
        installation_thread.append(threading.get_ident())
        return asyncio.run(service.reflect(original))

    event.listen(db.engine, "before_cursor_execute", hold_before_task_lock)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            installing = pool.submit(install)
            try:
                assert before_lock.wait(5)
                with db.session.begin() as session:
                    row = session.get(Task, original.id)
                    if change == "reclaimed":
                        row.lease_until = now() - timedelta(seconds=1)
                    else:
                        row.deadline = now() - timedelta(seconds=1)
                if change == "reclaimed":
                    replacement = runtime.claim("reflection-new", background=True)
                    assert replacement.id == original.id
                    assert replacement.lease_version == original.lease_version + 1
            finally:
                release.set()
            assert installing.result(timeout=5) == {"stale": True}
        with db.session() as session:
            assert session.scalar(select(RoleSnapshot.id).where(RoleSnapshot.actor_id == actor)) is None
            assert session.scalar(select(RoleMemory.id).where(
                RoleMemory.actor_id == actor, RoleMemory.kind == "commitment")) is None
            assert session.get(Operation, f"{original.id}:reflected") is None
    finally:
        release.set()
        event.remove(db.engine, "before_cursor_execute", hold_before_task_lock)
