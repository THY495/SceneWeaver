import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from support.story import event, launch, setup_story

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.knowledge import Knowledge
from chatbot.providers import ProviderError
from chatbot.runtime import StaleTask
from chatbot.storage import Chunk, Conversation, Document, Message, Story, Task, uid


def test_player_identity_is_idempotent_and_cast_is_frozen(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        url = f"/api/stories/{story}/player"
        assert client.post(url, json={"name": "访客"}).json()["id"] == player
        assert client.post(url, json={"name": "另一个人"}).status_code == 409
        card = client.post("/api/characters", json={"name": "医生", "core": "医生", "world": "雾港"}).json()
        assert (
            client.post(
                f"/api/characters/{card['id']}/sessions",
                json={
                    "version": 1,
                    "request_id": "late",
                    "story_id": story,
                },
            ).status_code
            == 400
        )
        for sid in [player, actors[0]]:
            assert (
                client.put(
                    f"/api/sessions/{sid}/profile",
                    json={
                        "name": "替身",
                        "core": "替身",
                        "world": "雾港",
                    },
                ).status_code
                == 400
            )
        assert (
            client.put(
                f"/api/sessions/{actors[0]}/scene-facts",
                json={
                    "key": "位置",
                    "value": "门外",
                    "event_id": "legacy",
                },
            ).status_code
            == 400
        )


async def test_sequential_cast_keeps_secrets_separate_and_rotates(db, config, runtime, fake):
    config.product = "roleplay"
    fake.delay = 0.02
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        assert client.post(f"/api/stories/{story}/events", json=event(actors[0])).status_code == 201
        hidden = {**event(player), "request_id": "player-private", "value": "玩家暗号紫色蜻蜓"}
        assert client.post(f"/api/stories/{story}/events", json=hidden).status_code == 201
    with db.session.begin() as s:
        s.get(Story, story).settings = {"chain_limit": 2}
        s.add(Message(session_id=actors[1], seq=80, role="user", content="私下对话金色蝴蝶"))
    fake.replies["role_intent"] = {
        "act": "speak",
        "purpose": "继续交流",
        "source_ids": [],
        "addressee_id": None,
        "wait_for_player": False,
        "deception": False,
    }
    runtime.submit(player, config.workspace_owner, "大家好", "p1")
    task = runtime.claim("w")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["actor_ids"] == actors[:2]
    contexts = [str(c[1]) for c in fake.calls if c[0] == "dialogue"]
    assert "铁盒" in contexts[0] and "铁盒" not in contexts[1]
    assert all("紫色蜻蜓" not in c and "金色蝴蝶" not in c for c in contexts)
    assert runtime.finish(task.id, task.lease_version, result)
    runtime.submit(player, config.workspace_owner, "继续", "p2")
    second = runtime.claim("w")
    next_result = await Agents(db, config, runtime, fake).respond(second)
    assert next_result["actor_ids"][0] == actors[2]
    assert runtime.finish(second.id, second.lease_version, next_result)
    runtime.submit(player, config.workspace_owner, "修复师，请回答", "p3")
    third = runtime.claim("w")
    final = await Agents(db, config, runtime, fake).respond(third)
    assert final["actor_ids"] == [actors[1]]


async def test_private_event_cancels_inflight_player_answer(db, config, runtime, fake):
    config.product = "roleplay"
    fake.delay = 0.1
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        runtime.submit(player, config.workspace_owner, "大家好", "pending")
        task = runtime.claim("w")
        job = asyncio.create_task(Agents(db, config, runtime, fake).respond(task))
        await asyncio.sleep(0.03)
        assert client.post(f"/api/stories/{story}/events", json=event(actors[0])).status_code == 201
        with pytest.raises(StaleTask):
            await job
    with db.session() as s:
        assert s.get(Task, task.id).status == "superseded"
        assert not list(
            s.scalars(select(Message).where(Message.session_id == player, Message.role == "assistant"))
        )


async def test_committed_segment_survives_later_actor_revision_change(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "馆长你好", "race")
    task = runtime.claim("w")
    result = await Agents(db, config, runtime, fake).respond(task)
    with db.session.begin() as s:
        s.get(Conversation, actors[0]).revision += 1
    assert runtime.finish(task.id, task.lease_version, result)
    with db.session() as s:
        assert s.get(Task, task.id).status == "completed"
        assert s.scalar(select(Message.id).where(Message.task_id == task.id, Message.role == "assistant"))


async def test_actor_retrieval_filters_before_ranking(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
        _, outsiders = setup_story(client)
    with db.session.begin() as s:
        for sid, word in zip(actors[:2], ["铁盒", "药箱"]):
            doc = Document(
                id=uid(),
                owner_id=config.workspace_owner,
                name=word,
                content_hash=word,
                blob_path="unused",
                version=1,
                status="ready",
            )
            s.add(doc)
            s.flush()
            s.add(
                Chunk(
                    document_id=doc.id,
                    version=1,
                    ordinal=0,
                    section="秘密",
                    content="账册在" + word,
                    terms="账册",
                    search_vector=func.to_tsvector("simple", "账册"),
                )
            )
            c = s.get(Conversation, sid)
            c.current_state = {
                **c.current_state,
                "profile": {**c.current_state["profile"], "document_ids": [doc.id]},
            }
    task = runtime.submit(player, config.workspace_owner, "账册", "scope")
    knowledge = Knowledge(db, config, runtime, fake)
    for sid, word in zip(actors[:2], ["铁盒", "药箱"]):
        hits = await knowledge.search(config.workspace_owner, "账册", task_id=task.id, actor_id=sid)
        assert len(hits) == 1 and word in hits[0]["text"]
    with pytest.raises(ValueError):
        await knowledge.search(config.workspace_owner, "账册", task_id=task.id, actor_id=outsiders[0])


async def test_rejected_draft_is_repaired_before_commit(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    drafts = iter(["我去年亲眼在柜子里见过。", "我没有亲眼确认过位置。"])
    reviews = iter([{"accepted": False, "issues": ["编造去年亲历"]}, {"accepted": True, "issues": []}])
    fake.replies.update(dialogue=lambda _: next(drafts), actor_review=lambda _: next(reviews))
    runtime.submit(player, config.workspace_owner, "馆长，你见过吗？", "repair")
    task = runtime.claim("w")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert "去年" not in result["answer"] and "没有亲眼" in result["answer"]
    assert len([call for call in fake.calls if call[0] != "role_intent"]) == 4
    assert runtime.finish(task.id, task.lease_version, result)


async def test_semantic_failure_falls_back_once_and_keeps_document_scope(
    db, config, runtime, fake, monkeypatch
):
    from chatbot.providers import Embeddings

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    with db.session.begin() as s:
        for aid, value in zip(actors[:2], ("铁盒", "私密药箱"), strict=True):
            doc = Document(
                id=uid(),
                owner_id=config.workspace_owner,
                name=value,
                content_hash=value,
                blob_path="unused",
                status="ready",
            )
            s.add(doc)
            s.flush()
            s.add(
                Chunk(
                    document_id=doc.id,
                    version=1,
                    ordinal=0,
                    section="资料",
                    content="账册在" + value,
                    terms="账册",
                    search_vector=func.to_tsvector("simple", "账册"),
                )
            )
            c = s.get(Conversation, aid)
            c.current_state = {
                **c.current_state,
                "profile": {**c.current_state["profile"], "document_ids": [doc.id]},
            }
    failures = []

    async def unavailable(self, texts, task_id):
        failures.append(task_id)
        raise ProviderError("network unavailable")

    monkeypatch.setattr(Embeddings, "encode", unavailable)
    config.embedding_provider = "api"
    runtime.submit(player, config.workspace_owner, "账册", "fallback")
    task = runtime.claim("w")
    knowledge = Knowledge(db, config, runtime, fake)
    for _ in range(2):
        hits = await knowledge.search(config.workspace_owner, "账册", task_id=task.id, actor_id=actors[0])
        assert (
            len(hits) == 1 and "铁盒" in hits[0]["text"] and hits[0]["retrieval_mode"] == "lexical_fallback"
        )
    assert len(failures) == 1


async def test_repeated_rejection_never_publishes_draft(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    fake.replies["actor_review"] = {"accepted": False, "issues": ["扮演了其他角色"]}
    runtime.submit(player, config.workspace_owner, "馆长，说点什么", "reject")
    task = runtime.claim("w")
    with pytest.raises(ProviderError, match="此片段未提交"):
        await Agents(db, config, runtime, fake).respond(task)
    with db.session() as s:
        assert not s.scalar(select(Message.id).where(Message.task_id == task.id, Message.role == "assistant"))
