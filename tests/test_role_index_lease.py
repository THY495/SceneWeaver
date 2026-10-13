"""Embedding results cannot outlive the task lease that produced them."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, update
from sqlalchemy.exc import OperationalError
from support.story import launch

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.role_index import index_memories, schedule_index
from chatbot.storage import RoleMemory, Task, now


def index_case(db, config, runtime, fake, monkeypatch):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
        runtime.submit(player, config.workspace_owner, "这份记录需要归档。", "index-lease-source",
                       channel="private", addressees=[actors[0]])
    config.embedding_provider = "api"
    scheduled = schedule_index(db, config, runtime)
    assert scheduled
    with db.session.begin() as session:
        session.execute(update(Task).where(Task.id != scheduled.id).values(status="cancelled"))
    task = runtime.claim("index-old", background=True)
    assert task.id == scheduled.id

    async def encode(self, texts, task_id):
        assert task_id == task.id and texts
        return [[0.1] * 1024 for _ in texts]

    monkeypatch.setattr("chatbot.role_index.Embeddings.encode", encode)
    return Agents(db, config, runtime, fake), task


@pytest.mark.parametrize("change", ["reclaimed", "deadline", "cancelled"])
def test_index_rechecks_task_after_outside_lease_check(db, config, runtime, fake, monkeypatch, change):
    agents, original = index_case(db, config, runtime, fake, monkeypatch)
    checked, release = threading.Event(), threading.Event()
    original_check = runtime.check

    def hold_after_check(task_id, version):
        result = original_check(task_id, version)
        checked.set()
        if not release.wait(5):
            raise TimeoutError("Index installation test gate timed out")
        return result

    monkeypatch.setattr(runtime, "check", hold_after_check)
    with ThreadPoolExecutor(max_workers=1) as pool:
        installing = pool.submit(lambda: asyncio.run(index_memories(agents, original)))
        try:
            assert checked.wait(5), "Indexer never reached the outside lease check"
            with db.session.begin() as session:
                row = session.get(Task, original.id)
                if change == "reclaimed":
                    row.lease_until = now() - timedelta(seconds=1)
                elif change == "deadline":
                    row.deadline = now() - timedelta(seconds=1)
                else:
                    row.status = "cancelled"
            if change == "reclaimed":
                replacement = runtime.claim("index-new", background=True)
                assert replacement.id == original.id
                assert replacement.lease_version == original.lease_version + 1
        finally:
            release.set()
        result = installing.result(timeout=5)
    with db.session() as session:
        installed = list(session.scalars(select(RoleMemory.id).where(
            RoleMemory.id.in_(original.payload["ids"]), RoleMemory.embedding.is_not(None))))
    assert installed == [], {"change": change, "result": result, "late_installations": len(installed)}
    assert result == {"stale": True}


def test_index_installation_holds_lease_until_vector_commit(db, config, runtime, fake, monkeypatch):
    agents, original = index_case(db, config, runtime, fake, monkeypatch)
    at_row, release = threading.Event(), threading.Event()
    installation_thread = []

    def hold_at_install_row(conn, cursor, statement, parameters, context, many):
        if (installation_thread and threading.get_ident() == installation_thread[0]
            and statement.lstrip().startswith("SELECT role_memories.id")
            and any(mid in parameters.values() for mid in original.payload["ids"])
            and len(parameters) == 1):
            at_row.set()
            if not release.wait(5):
                raise TimeoutError("Index row installation gate timed out")

    def install():
        installation_thread.append(threading.get_ident())
        return asyncio.run(index_memories(agents, original))

    event.listen(db.engine, "after_cursor_execute", hold_at_install_row)
    fenced = False
    replacement = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            installing = pool.submit(install)
            try:
                assert at_row.wait(5), "Indexer never reached its installation row"
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
                    replacement = runtime.claim("index-new", background=True)
                    assert replacement.id == original.id
                    assert replacement.lease_version == original.lease_version + 1
            finally:
                release.set()
            result = installing.result(timeout=5)
        with db.session() as session:
            installed = list(session.scalars(select(RoleMemory.id).where(
                RoleMemory.id.in_(original.payload["ids"]), RoleMemory.embedding.is_not(None))))
        assert fenced, {"replacement_claimed": replacement is not None, "late_installations": len(installed)}
        assert result == {"indexed": len(original.payload["ids"])}
        assert len(installed) == result["indexed"]
        assert runtime.finish(original.id, original.lease_version, result)
    finally:
        release.set()
        event.remove(db.engine, "after_cursor_execute", hold_at_install_row)
