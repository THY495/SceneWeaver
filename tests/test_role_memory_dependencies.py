"""Reflection notes may depend on more than their quoted evidence anchor."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from support.story import launch

from chatbot.api import create_app
from chatbot.memory_dependencies import memory_visible, migrate_dependencies, visible_memory_clause
from chatbot.observations import append_record, visible_records
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, RoleMemory, RoleSnapshot, SceneRecord, uid


def speech(db, story, speaker, observer, key, text):
    with db.session.begin() as session:
        return append_record(session, story_id=story, kind="speech", text=text,
                             actor=session.get(Conversation, speaker),
                             observer_ids=[speaker, observer] if speaker != observer else [speaker],
                             dedupe=key).id


async def reflect(service, runtime, actor, owner):
    scheduled = service.schedule(actor, owner, force=True)
    task = runtime.claim("dependency-reflection", background=True)
    assert task.id == scheduled.id
    result = await service.reflect(task)
    assert runtime.finish(task.id, task.lease_version, result)
    return result


async def test_note_anchor_survives_but_deleted_snapshot_dependency_cannot(db, config, runtime, fake, tmp_path):
    config.product = "roleplay"
    config.context_hard_tokens = 24000
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        actor = actors[0]
        secret = speech(db, story, player, actor, "private-origin", "抽屉暗号是四一七；只是转述。")
        for index in range(4):
            speech(db, story, actor, actor, f"old-{index}", "还有记录需要核对。")
        service = RoleMemoryService(db, config, runtime, fake)
        fake.replies["role_memory"] = {"summary": "玩家曾转述暗号四一七，未核实。", "notes": []}
        await reflect(service, runtime, actor, config.workspace_owner)
        correction = "我纠正：银钥匙在档案柜，这仍是转述。"
        anchor = speech(db, story, player, actor, "surviving-anchor", correction)
        for index in range(4):
            speech(db, story, actor, actor, f"later-{index}", "继续记录待核实事项。")
        fake.replies["role_memory"] = {"summary": "玩家纠正了钥匙的位置，尚待核实。", "notes": [{
            "kind": "belief", "content": "地点已改口，四一七这个暗号仍待查证。",
            "source_id": anchor, "quote": correction, "same_as": None,
        }]}
        await reflect(service, runtime, actor, config.workspace_owner)
        task = runtime.submit(player, config.workspace_owner, "现有记录能确认什么？", "check-dependency",
                              channel="private", addressees=[actor])
        before, _ = service.context(task, actor)
        assert "四一七" in json.dumps(before, ensure_ascii=False)
        client.delete(f"/api/role/stories/{story}/events/{secret}").raise_for_status()
        with db.session() as session:
            assert anchor in {event.id for event in visible_records(session, actor)}
            notes = [{"id": row.id, "status": row.status, "content": row.content, "anchors": row.source_ids}
                     for row in session.scalars(select(RoleMemory).where(RoleMemory.actor_id == actor,
                                                                        RoleMemory.kind == "belief"))]
        (tmp_path / "deletion-receipt.json").write_text(json.dumps(notes, ensure_ascii=False, indent=2), "utf-8")
        after, _ = service.context(task, actor)
        assert "四一七" not in json.dumps(after, ensure_ascii=False)


async def test_existing_note_dependencies_reach_new_note_and_same_as_archive(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        actor = actors[0]
        secret = speech(db, story, player, actor, "dependency", "玩家转述了暗号四一七。")
        anchor_text = "我还需要核对记录。"
        anchor = speech(db, story, actor, actor, "anchor", anchor_text)
        old_id = uid()
        with db.session.begin() as session:
            session.add(RoleMemory(id=old_id, story_id=story, actor_id=actor, kind="belief",
                content="暗号四一七尚未核实。", source_ids=[anchor], context_source_ids=[secret, anchor]))
        for index in range(4):
            speech(db, story, actor, actor, f"filler-{index}", "继续核对。")
        fake.replies["role_memory"] = {"summary": "记录仍待核对。", "notes": [
            {"kind": "belief", "content": "新增表述", "source_id": anchor,
             "quote": anchor_text, "same_as": old_id},
            {"kind": "impression", "content": "四一七只是玩家转述。", "source_id": anchor,
             "quote": anchor_text, "same_as": None},
        ]}
        service = RoleMemoryService(db, config, runtime, fake)
        result = await reflect(service, runtime, actor, config.workspace_owner)
        assert result["notes"] == 1
        with db.session() as session:
            active = session.get(RoleMemory, old_id)
            archived = session.get(RoleMemory, active.replaces_id)
            derived = session.scalar(select(RoleMemory).where(RoleMemory.actor_id == actor,
                                                             RoleMemory.kind == "impression"))
            snapshot = session.scalar(select(RoleSnapshot).where(RoleSnapshot.actor_id == actor))
            assert active.version == 2 and active.content == "暗号四一七尚未核实。"
            assert active.source_ids == [anchor]  # Dependencies-only merges still version the note.
            assert set(archived.context_source_ids) == {secret, anchor}
            assert archived.status == "archived" and archived.version == 1
            assert secret in derived.context_source_ids and secret in snapshot.source_ids
            assert derived.source_ids == [anchor]
        client.delete(f"/api/role/stories/{story}/events/{secret}").raise_for_status()
        with db.session() as session:
            for mid in (old_id, archived.id, derived.id):
                deleted = session.get(RoleMemory, mid)
                assert deleted.status == "deleted" and "四一七" not in deleted.content
            assert anchor in {e.id for e in visible_records(session, actor)}
        body = client.get(f"/api/role/actors/{actor}/memories?include_archived=true").json()
        assert "四一七" not in json.dumps(body, ensure_ascii=False)


async def test_dependency_revalidated_after_model_returns_even_without_privacy_change(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        actor = actors[0]
        dependency = speech(db, story, player, actor, "late-delete", "旧消息中有暗号四一七。")
        anchor_text = "这件事我只听说过。"
        anchor = speech(db, story, actor, actor, "new-anchor", anchor_text)
        with db.session.begin() as session:
            session.add(RoleMemory(id=uid(), story_id=story, actor_id=actor, kind="belief",
                content="四一七是传闻。", source_ids=[anchor], context_source_ids=[dependency, anchor]))
        for index in range(4):
            speech(db, story, actor, actor, f"pending-{index}", "还有记录。")
        entered, release = asyncio.Event(), asyncio.Event()
        original = fake.chat
        fake.replies["role_memory"] = {"summary": "四一七是传闻。", "notes": [{
            "kind": "impression", "content": "四一七待查。", "source_id": anchor,
            "quote": anchor_text, "same_as": None,
        }]}

        async def hold(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        fake.chat = hold
        service = RoleMemoryService(db, config, runtime, fake)
        running = asyncio.create_task(reflect(service, runtime, actor, config.workspace_owner))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            with db.session.begin() as session:
                privacy = session.get(Conversation, actor).privacy_version
                # Deliberately bypass the API's version increment: installation must independently
                # recheck visibility, rather than relying only on the privacy version guard.
                event = session.get(SceneRecord, dependency)
                event.redacted, event.text, event.payload = True, "[已删除]", {}
            release.set()
            assert await running == {"stale": True}
            with db.session() as session:
                assert session.get(Conversation, actor).privacy_version == privacy
                assert session.scalar(select(RoleSnapshot.id).where(RoleSnapshot.actor_id == actor)) is None
                assert session.scalar(select(RoleMemory.id).where(RoleMemory.actor_id == actor,
                                                                 RoleMemory.kind == "impression")) is None
                visible = {e.id for e in visible_records(session, actor)}
                assert not list(session.scalars(select(RoleMemory).where(RoleMemory.actor_id == actor,
                    RoleMemory.kind == "belief", visible_memory_clause(visible))))
        finally:
            release.set()
            if not running.done():
                running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            fake.chat = original


def test_legacy_upgrade_preserves_unknown_notes_but_never_reads_them(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = launch(client)
        actor = actors[0]
        authored = client.post(f"/api/role/actors/{actor}/memories", json={
            "kind": "belief", "content": "我对钥匙去向尚不确定。", "request_id": "legacy-authored",
        })
        authored.raise_for_status()
        direct_id = authored.json()["id"]
        with db.session.begin() as session:
            direct = session.get(RoleMemory, direct_id)
            unknown_id = uid()
            session.add(RoleMemory(id=unknown_id, story_id=story, actor_id=actor, kind="impression",
                content="四一七（引用作者记录但另有未知输入）。", source_ids=list(direct.source_ids)))
        with db.engine.begin() as connection:
            # Reproduce an actual old schema, then run the shared native/Alembic migration twice.
            connection.execute(text("ALTER TABLE role_memories DROP COLUMN context_source_ids"))
            migrate_dependencies(connection)
            migrate_dependencies(connection)
        with db.session() as session:
            visible = {e.id for e in visible_records(session, actor)}
            direct = session.get(RoleMemory, direct_id)
            unknown = session.get(RoleMemory, unknown_id)
            assert direct.context_source_ids == direct.source_ids and memory_visible(direct, visible)
            assert unknown.context_source_ids is None and unknown.status == "active"
            assert "四一七" in unknown.content and not memory_visible(unknown, visible)
            episodes = list(session.scalars(select(RoleMemory).where(RoleMemory.actor_id == actor,
                                                                    RoleMemory.kind == "episode")))
            assert episodes and all(m.context_source_ids == m.source_ids for m in episodes)
            ids = set(session.scalars(select(RoleMemory.id).where(visible_memory_clause(visible))))
            assert direct_id in ids and unknown_id not in ids
        body = client.get(f"/api/role/actors/{actor}/memories").json()
        assert direct_id in {row["id"] for row in body["memories"]}
        assert unknown_id not in {row["id"] for row in body["memories"]}
        updated = client.patch(f"/api/role/actors/{actor}/memories/{unknown_id}", json={
            "expected_version": 1, "status": "archived",
        })
        assert updated.status_code == 409
        with db.session() as session:
            assert not list(session.scalars(select(SceneRecord).where(
                SceneRecord.story_id == story, SceneRecord.kind == "memory_change")))


@pytest.mark.parametrize("legacy_event", [False, True])
def test_status_change_event_inherits_dependencies_and_cannot_restore_deleted_secret(db, config, runtime, fake,
                                                                                   legacy_event):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        actor = actors[0]
        secret = speech(db, story, player, actor, "changed-secret", "暗号四一七是玩家转述。")
        anchor = speech(db, story, actor, actor, "changed-anchor", "我会核对记录。")
        mid = uid()
        with db.session.begin() as session:
            session.add(RoleMemory(id=mid, story_id=story, actor_id=actor, kind="belief",
                content="四一七尚未核实。", source_ids=[anchor], context_source_ids=[secret, anchor]))
        response = client.patch(f"/api/role/actors/{actor}/memories/{mid}", json={
            "expected_version": 1, "status": "archived",
        })
        response.raise_for_status()
        with db.session() as session:
            changed = session.scalar(select(SceneRecord).where(SceneRecord.story_id == story,
                                                               SceneRecord.kind == "memory_change"))
            assert changed and "四一七" in changed.text
            assert set(changed.payload["context_source_ids"]) == {secret, anchor}
        if legacy_event:
            with db.session.begin() as session:
                row = session.get(SceneRecord, changed.id)
                row.payload = {k: v for k, v in row.payload.items() if k != "context_source_ids"}
        client.delete(f"/api/role/stories/{story}/events/{secret}").raise_for_status()
        with db.session() as session:
            visible = {e.id for e in visible_records(session, actor)}
            assert anchor in visible and changed.id not in visible
            assert session.get(RoleMemory, mid).status == "deleted"
        task = runtime.submit(player, config.workspace_owner, "核对仍有效的记录。", "after-status-redaction",
                              channel="private", addressees=[actor])
        messages, _ = RoleMemoryService(db, config, runtime, fake).context(task, actor)
        assert "四一七" not in json.dumps(messages, ensure_ascii=False)
