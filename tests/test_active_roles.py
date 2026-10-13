import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch, persona

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.observations import append_record, record_data, visible_records
from chatbot.providers import ProviderError
from chatbot.role_memory import RoleMemoryService
from chatbot.runtime import StaleTask
from chatbot.storage import (
    Conversation,
    Message,
    RoleMemory,
    SceneRecord,
    Task,
    now,
)


def test_archived_facts_remain_retrievable_and_updates_require_current_source(db, config, runtime, fake):
    from support.story import event

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        first = None
        for i in range(35):
            body = {
                **event(actors[0]),
                "request_id": f"archive-{i}",
                "key": f"档案{i:03d}",
                "value": f"档案{i:03d}存于专用柜{i:03d}",
            }
            response = client.post(f"/api/stories/{story}/events", json=body)
            assert response.status_code == 201, response.text
            if i == 0:
                first = response.json()["id"]
        task = runtime.submit(player, config.workspace_owner, "档案000在哪里", "recall")
        context, _ = RoleMemoryService(db, config, runtime, fake).context(task, actors[0])
        assert context[0]["content"].startswith("# 身份与表达\n你是角色档案中的人物")
        assert "通用助手" not in context[0]["content"]
        assert "连续对话助手" not in context[0]["content"]
        state = json.loads(context[1]["content"].partition("：")[2])["state"]
        assert state["scene_facts"]["档案000"]["event_id"] == first
        with db.session() as s:
            assert len(s.get(Conversation, actors[0]).current_state["scene_facts"]) == 30
        update = {**event(actors[0]), "request_id": "replace-old", "key": "档案000", "value": "移至新柜"}
        assert client.post(f"/api/stories/{story}/events", json=update).status_code == 409
        update["recipients"][0]["replaces_event_id"] = first
        assert client.post(f"/api/stories/{story}/events", json=update).status_code == 201


def test_many_required_sources_compact_metadata_without_dropping_commitments(db, config, runtime, fake):
    from chatbot.observations import append_record
    from chatbot.storage import RoleSnapshot

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    ids = []
    with db.session.begin() as s:
        actor = s.get(Conversation, actors[0])
        for i in range(15):
            event = append_record(
                s,
                story_id=story,
                kind="speech",
                actor=actor,
                observer_ids=[actor.id],
                dedupe=f"promise-{i}",
                text=f"我答应提醒第{i}件申请。",
                payload={"intent": {"purpose": "仅供角色自己的历史诊断。" * 100}},
            )
            ids.append(event.id)
            s.add(
                RoleMemory(
                    story_id=story,
                    actor_id=actor.id,
                    kind="commitment",
                    content=f"提醒第{i}件申请",
                    source_ids=[event.id],
                )
            )
        s.add(
            RoleSnapshot(
                actor_id=actor.id,
                covered_seq=event.seq,
                privacy_version=actor.privacy_version,
                source_ids=ids,
                summary={"summary": "保留申请提醒"},
            )
        )
    task = runtime.submit(player, config.workspace_owner, "请提醒申请事项。", "many-required")
    context, info = RoleMemoryService(db, config, runtime, fake).context(task, actors[0])
    background = json.loads(context[1]["content"].partition("：")[2])
    assert background["archived_metadata_omitted"]
    assert set(ids).issubset({e["id"] for e in background["recalled_observations"]})
    assert len(info["active_goals"]) == 15
    assert info["tokens"] <= config.context_hard_tokens - 6000


def test_statement_trace_recalls_player_correction_over_repeated_npc_paraphrases(db, config, runtime, fake):
    from chatbot.observations import append_record
    from chatbot.storage import RoleSnapshot

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    originals = []
    with db.session.begin() as s:
        visitor = s.get(Conversation, player)
        npc = s.get(Conversation, actors[0])
        for i, value in enumerate(
            (
                "有人说原册在医药箱里。",
                "有人说原册在医药箱里。",
                "我纠正一下，医药箱只是转述，不是我亲眼见过。",
            )
        ):
            originals.append(
                append_record(
                    s,
                    story_id=story,
                    kind="speech",
                    text=value,
                    observer_ids=[actors[0]],
                    actor=visitor,
                    dedupe=f"original-{i}",
                ).id
            )
        hidden = append_record(
            s,
            story_id=story,
            kind="speech",
            text="我纠正：医药箱里有私密密码。",
            observer_ids=[actors[1]],
            actor=visitor,
            dedupe="hidden",
        )
        for i in range(30):
            append_record(
                s,
                story_id=story,
                kind="speech",
                actor=npc,
                observer_ids=[actors[0]],
                text="我两次对医药箱的说法都只能确认铁盒实际位置。",
                dedupe=f"repeat-{i}",
            )
        for i in range(12):
            append_record(
                s,
                story_id=story,
                kind="speech",
                actor=visitor,
                observer_ids=[actors[0]],
                dedupe=f"repeat-question-{i}",
                text=f"第{i}次核对，我两次对医药箱的说法分别是什么，能确认实际位置吗？",
            )
        records = visible_records(s, actors[0])
        s.add(
            RoleSnapshot(
                actor_id=actors[0],
                covered_seq=records[-1].seq,
                privacy_version=npc.privacy_version,
                source_ids=[e.id for e in records],
                summary={"summary": "多次讨论医药箱与铁盒"},
            )
        )
        hidden_id = hidden.id
    task = runtime.submit(player, config.workspace_owner, "我两次对医药箱的说法分别是什么？", "trace")
    context, info = RoleMemoryService(db, config, runtime, fake).context(task, actors[0])
    available = {event["id"] for event in info["observations"]}
    assert set(originals).issubset(available)
    assert hidden_id not in available and "私密密码" not in str(context)


async def test_semantic_memory_recall_is_scoped_before_ranking(db, config, runtime, fake, monkeypatch):
    from chatbot.observations import append_record
    from chatbot.providers import Embeddings
    from chatbot.storage import RoleSnapshot, uid

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    with db.session.begin() as s:
        ids = []
        for aid, text in [(actors[0], "临时约定使用铜铃"), (actors[1], "不可见的紫色密码")]:
            e = append_record(s, story_id=story, kind="speech", text=text, observer_ids=[aid], dedupe=aid)
            ids.append(e.id)
        s.flush()
        for m in s.scalars(select(RoleMemory)):
            m.embedding = [1.0] + [0.0] * 1023
            m.embedding_model = config.embedding_model
        s.add(
            RoleSnapshot(
                id=uid(),
                actor_id=actors[0],
                covered_seq=2,
                privacy_version=0,
                source_ids=[ids[0]],
                summary={"summary": "过去有过交流"},
            )
        )
        # Use the actual privacy version from the actor.
        s.flush()
        for snap in s.scalars(select(RoleSnapshot)):
            snap.privacy_version = s.get(Conversation, actors[0]).privacy_version
    calls = []

    async def encode(self, texts, task_id):
        calls.append(texts)
        return [[1.0] + [0.0] * 1023]

    monkeypatch.setattr(Embeddings, "encode", encode)
    config.embedding_provider = "api"
    runtime.submit(player, config.workspace_owner, "提醒方式", "vector")
    task = runtime.claim("w")
    memory = RoleMemoryService(db, config, runtime, fake)
    await memory.prepare_recall(task, actors[0])
    await memory.prepare_recall(task, actors[1])
    context, info = memory.context(task, actors[0])
    assert "临时约定使用铜铃" in str(context) and "紫色密码" not in str(context)
    assert len(calls) == 1 and ids[0] in {e["id"] for e in info["observations"]}


async def test_48_turns_personal_snapshots_keep_goal_and_originals(db, config, runtime, fake):
    from chatbot.storage import RoleSnapshot

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
        goal = client.post(
            f"/api/role/actors/{actors[0]}/memories", json={"kind": "commitment", "content": "我答应归还银铃"}
        ).json()["id"]
    memory = RoleMemoryService(db, config, runtime, fake)
    for i in range(48):
        task = runtime.submit(
            player,
            config.workspace_owner,
            f"馆长，第{i}轮继续交流",
            f"long-{i}",
            channel="private",
            addressees=[actors[0]],
        )
        task = runtime.claim("w")
        result = await Agents(db, config, runtime, fake).respond(task)
        runtime.finish(task.id, task.lease_version, result)
        if i % 6 == 5:
            pending = memory.schedule(actors[0], config.workspace_owner, force=True)
            assert pending
            reflection = runtime.claim("r", background=True)
            assert reflection.kind == "role_reflect"
            runtime.finish(reflection.id, reflection.lease_version, await memory.reflect(reflection))
    with db.session() as s:
        assert len(list(s.scalars(select(RoleSnapshot).where(RoleSnapshot.actor_id == actors[0])))) == 8
        assert s.get(RoleMemory, goal).status == "active"
        assert len(visible_records(s, actors[0])) == 97 and not visible_records(s, actors[1])
    context, info = memory.context(task, actors[0])
    assert "我答应归还银铃" in str(context) and info["snapshot_id"]


def test_relation_change_history_and_submit_clears_typing(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        people = client.get(f"/api/role/stories/{story}/roster").json()
        revision = next(p["revision"] for p in people if p["id"] == actors[0])
        body = {
            "expected_revision": revision,
            "target": "修复师",
            "relation": "合作伙伴",
            "status": "current",
            "basis": "共同签署协议",
        }
        result = client.post(f"/api/role/actors/{actors[0]}/relationships", json=body)
        assert result.status_code == 201
        assert client.post(f"/api/role/actors/{actors[0]}/relationships", json=body).status_code == 409
        client.post(f"/api/role/stories/{story}/presence", json={"visible": True, "typing": True})
        runtime.submit(player, config.workspace_owner, "现在发送", "typing")
        with db.session() as s:
            assert s.get(Conversation, player).current_state["presence"]["typing"] is False
            assert all(e.kind != "relationship" for e in visible_records(s, actors[1]))
        assert client.delete(f"/api/role/stories/{story}/events/{result.json()['id']}").status_code == 200
        with db.session() as s:
            assert not any(
                r["target"] == "修复师"
                for r in s.get(Conversation, actors[0]).current_state["profile"]["relationships"]
            )


def intent(target=None, wait=True, purpose="交流"):
    return {
        "act": "speak",
        "purpose": purpose,
        "addressee_id": target,
        "source_ids": [],
        "wait_for_player": wait,
        "deception": False,
    }


async def test_lookup_executor_completes_only_explicit_lookup_goal(db, config, runtime, fake, monkeypatch):
    from chatbot.knowledge import Knowledge
    from chatbot.storage import Document, uid

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
        goal = client.post(
            f"/api/role/actors/{actors[0]}/memories", json={"kind": "goal", "content": "查阅资料：账册登记"}
        ).json()["id"]
        other = client.post(
            f"/api/role/actors/{actors[0]}/memories", json={"kind": "goal", "content": "找到真正的失踪原因"}
        ).json()["id"]
    with db.session.begin() as s:
        doc = Document(
            id=uid(),
            owner_id=config.workspace_owner,
            name="资料",
            content_hash="x",
            blob_path="unused",
            status="ready",
        )
        s.add(doc)
        c = s.get(Conversation, actors[0])
        c.current_state = {
            **c.current_state,
            "profile": {**c.current_state["profile"], "document_ids": [doc.id]},
        }
    inspected = []

    async def lookup(self, task, question, **kwargs):
        inspected.append(kwargs["actor_id"])
        return {"evidence": [], "missing": ["没有相关记录"]}

    monkeypatch.setattr(Knowledge, "retrieve", lookup)
    fake.replies["role_intent"] = {**intent(player), "act": "inspect", "goal_id": goal}
    runtime.submit(player, config.workspace_owner, "馆长，请查阅资料", "lookup")
    task = runtime.claim("w")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["segments"] and inspected == [actors[0]]
    assert "0段" in result["answer"]
    with db.session() as s:
        assert s.get(RoleMemory, goal).status == "completed"
        assert s.get(RoleMemory, other).status == "active"


async def test_intent_retry_and_local_evidence_handles_are_canonicalized(db, config, runtime, fake):
    from sqlalchemy import func

    from chatbot.storage import Chunk, Document, uid

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    with db.session.begin() as s:
        doc = Document(
            id=uid(),
            owner_id=config.workspace_owner,
            name="账册档案",
            content_hash="x",
            blob_path="unused",
            status="ready",
        )
        s.add(doc)
        s.flush()
        s.add(
            Chunk(
                id=uid(),
                document_id=doc.id,
                version=1,
                ordinal=0,
                section="位置",
                content="账册在铁盒。",
                terms="账册",
                search_vector=func.to_tsvector("simple", "账册"),
            )
        )
        c = s.get(Conversation, actors[0])
        c.current_state = {
            **c.current_state,
            "profile": {**c.current_state["profile"], "document_ids": [doc.id]},
        }
    attempts = iter([{**intent(player), "source_ids": ["forged-event"]}, intent(player)])
    fake.replies["role_intent"] = lambda _: next(attempts)
    fake.replies["dialogue"] = {
        "text": "账册在铁盒。",
        "claims": [{"text": "账册在铁盒。", "source_id": "S1", "quote": "账册在铁盒。"}],
    }
    runtime.submit(player, config.workspace_owner, "馆长，账册在哪", "handles")
    task = runtime.claim("w")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert len([call for call in fake.calls if call[0] == "role_intent"]) == 2
    with db.session() as s:
        record = s.get(SceneRecord, result["segments"][0]["event_id"])
        assert record.payload["claims"][0]["source_id"] == result["evidence"][0]["id"]
        assert not s.get(Conversation, actors[0]).current_state["scene_facts"]


async def test_large_retrieval_leaves_capacity_for_speech_review_and_repair(db, config, runtime, fake):
    from sqlalchemy import func

    from chatbot.providers import estimate_tokens
    from chatbot.storage import Chunk, Document, uid

    config.product = "roleplay"
    config.context_hard_tokens = 12000
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    with db.session.begin() as s:
        doc = Document(
            id=uid(),
            owner_id=config.workspace_owner,
            name="长档案",
            content_hash="large",
            blob_path="unused",
            status="ready",
        )
        s.add(doc)
        s.flush()
        for index in range(8):
            s.add(
                Chunk(
                    id=uid(),
                    document_id=doc.id,
                    version=1,
                    ordinal=index,
                    section="档案",
                    content=(f"账册条目{index}：资料中的引语“尚未确认”。\n" * 120),
                    terms="账册",
                    search_vector=func.to_tsvector("simple", "账册"),
                )
            )
        actor = s.get(Conversation, actors[0])
        actor.current_state = {
            **actor.current_state,
            "profile": {**actor.current_state["profile"], "document_ids": [doc.id]},
        }
    fake.replies["role_intent"] = intent(player)
    fake.replies["dialogue"] = {"text": "我需要先核对资料。", "claims": []}
    reviews = iter([{"accepted": False, "issues": ["请回应当前问题"]}, {"accepted": True, "issues": []}])
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "馆长，查一下账册", "large-review")
    task = runtime.claim("w")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["evidence"] and result["segments"]
    assert len([call for call in fake.calls if call[0] == "actor_review"]) == 2
    for role, messages, kwargs in fake.calls:
        assert estimate_tokens(messages) + kwargs.get("max_tokens", 0) <= config.context_hard_tokens, role


async def test_completed_startup_goal_not_resurrected_by_reflection(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        card = client.post(
            "/api/characters", json={"name": "甲", "core": "成年角色。\n当前目标：整理登记。"}
        ).json()
        c = client.post(
            f"/api/characters/{card['id']}/sessions", json={"version": 1, "request_id": "seed"}
        ).json()
        aid = c["id"]
        player = client.post(f"/api/stories/{c['story_id']}/player", json={"name": "访客"}).json()["id"]
        with db.session() as s:
            goal = s.scalar(select(RoleMemory).where(RoleMemory.actor_id == aid, RoleMemory.kind == "goal"))
            source = goal.source_ids[0]
        assert (
            client.patch(
                f"/api/role/actors/{aid}/memories/{goal.id}",
                json={"status": "completed", "expected_version": 1},
            ).status_code
            == 200
        )
        for i in range(3):
            runtime.submit(player, config.workspace_owner, f"你好{i}", f"seed-{i}")
    fake.replies["role_memory"] = {
        "summary": "先前目标已完成。",
        "notes": [{"kind": "goal", "content": "继续整理登记", "source_id": source, "quote": "整理登记"}],
    }
    service = RoleMemoryService(db, config, runtime, fake)
    service.schedule(aid, config.workspace_owner, force=True)
    task = runtime.claim("r", background=True)
    result = await service.reflect(task)
    assert result["notes"] == 0
    with db.session() as s:
        assert not s.scalar(
            select(RoleMemory.id).where(
                RoleMemory.actor_id == aid, RoleMemory.kind == "goal", RoleMemory.status == "active"
            )
        )


async def test_private_speech_is_an_event_not_a_world_fact(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        task = runtime.submit(
            player,
            config.workspace_owner,
            "暗号是青色松鼠",
            "private",
            channel="private",
            addressees=[actors[0]],
            occurred_at="霜月12日18:02",
        )
        assert runtime.submit(player, config.workspace_owner, "暗号是青色松鼠", "private").id == task.id
        task = runtime.claim("worker")
        result = await Agents(db, config, runtime, fake).respond(task)
        assert result["actor_ids"] == [actors[0]]
        assert runtime.finish(task.id, task.lease_version, result)
        a = client.get(f"/api/role/stories/{story}/timeline", params={"actor_id": actors[0]}).json()["events"]
        b = client.get(f"/api/role/stories/{story}/timeline", params={"actor_id": actors[1]}).json()["events"]
        assert len(a) == 2 and not b
        assert a[0]["speaker"] == "访客" and a[0]["occurred_at"] == "霜月12日18:02"
        assert a[0]["recorded_at"] and a[1]["parent_id"] == a[0]["id"]
        with db.session() as s:
            assert not s.get(Conversation, actors[0]).current_state["scene_facts"]
            assert len(list(s.scalars(select(RoleMemory).where(RoleMemory.actor_id == actors[0])))) == 2
            assert not list(s.scalars(select(RoleMemory).where(RoleMemory.actor_id == actors[1])))


async def test_npcs_react_to_committed_speech_without_reading_private_intent(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    fake.replies["role_intent"] = lambda messages: (
        intent(actors[1], True, "不能让别人知道地下暗门") if persona(messages) == "馆长" else intent(player)
    )
    fake.replies["dialogue"] = lambda messages: (
        "修复师，你听谁说的？" if persona(messages) == "馆长" else "我是听访客转述的。"
    )
    runtime.submit(player, config.workspace_owner, "你们聊聊", "chain")
    task = runtime.claim("worker")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["actor_ids"] == actors[:2] and result["stop_reason"] == "player_turn"
    b_inputs = [
        str(messages)
        for role, messages, _ in fake.calls
        if role in {"role_intent", "dialogue"} and persona(messages) == "修复师"
    ]
    assert all("修复师，你听谁说的？" in value for value in b_inputs)
    assert all("地下暗门" not in value for value in b_inputs)
    with db.session() as s:
        events = list(
            s.scalars(select(SceneRecord).where(SceneRecord.story_id == story).order_by(SceneRecord.seq))
        )
        assert len(events) == 3 and events[2].parent_id == events[1].id
        assert events[1].payload["intent"]["purpose"] == "不能让别人知道地下暗门"
        assert "地下暗门" not in str(record_data(events[1]))
        assert "地下暗门" in str(record_data(events[1], viewer_id=actors[0]))
    # Simulate a worker retry before task finalization: committed replies are not repeated.
    again = await Agents(db, config, runtime, fake).respond(task)
    assert again["actor_ids"] == actors[:2]
    with db.session() as s:
        assert len(list(s.scalars(select(SceneRecord).where(SceneRecord.task_id == task.id)))) == 3


async def test_private_group_uses_mentioned_order_not_character_creation_order(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    fake.replies["role_intent"] = lambda messages: (
        intent(actors[0], True) if persona(messages) == "修复师" else intent(player)
    )
    fake.replies["dialogue"] = lambda messages: (
        "馆长，你愿意先讨论哪一项？" if persona(messages) == "修复师" else "先谈目录吧。"
    )
    runtime.submit(
        player,
        config.workspace_owner,
        "修复师，请先问馆长一个问题。",
        "private-order",
        channel="private",
        addressees=actors[:2],
    )
    task = runtime.claim("worker")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["actor_ids"] == [actors[1], actors[0]]
    assert result["stop_reason"] == "player_turn"
    with db.session() as s:
        events = list(s.scalars(select(SceneRecord).where(SceneRecord.task_id == task.id)))
        assert all(event.channel == "private" for event in events)


async def test_submitted_input_survives_typing_and_revision_fence_before_commit(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        runtime.submit(player, config.workspace_owner, "馆长你好", "pause")
        task = runtime.claim("w")
        client.post(f"/api/role/stories/{story}/presence", json={"visible": True, "typing": True})
        result = await Agents(db, config, runtime, fake).respond(task)
        assert result["stop_reason"] == "player_turn" and result["actor_ids"]
        assert fake.calls
        runtime.finish(task.id, task.lease_version, result)
        client.post(f"/api/role/stories/{story}/presence", json={"visible": True, "typing": False})
    runtime.submit(player, config.workspace_owner, "馆长你好", "race")
    task = runtime.claim("w")

    def mutate(_):
        with db.session.begin() as s:
            s.get(Conversation, actors[0]).revision += 1
        return {"accepted": True, "issues": []}

    fake.replies["actor_review"] = mutate
    with pytest.raises(StaleTask):
        await Agents(db, config, runtime, fake).respond(task)
    with db.session() as s:
        assert not s.scalar(select(Message.id).where(Message.task_id == task.id, Message.role == "assistant"))


@pytest.mark.parametrize("voice_enabled", [False, True])
def test_idle_is_bounded_requires_goal_and_stops_outside_foreground(db, config, runtime, fake, voice_enabled):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        url = f"/api/role/stories/{story}/presence"
        with db.session.begin() as s:
            c = s.get(Conversation, player)
            c.current_state = {**c.current_state, "last_input_at": now().timestamp() - 100}
        assert client.post(url, json={"visible": True, "typing": False}).json()["task_id"] is None
        assert (
            client.post(
                f"/api/role/actors/{actors[0]}/memories", json={"kind": "goal", "content": "问清访客来意"}
            ).status_code
            == 201
        )
        assert client.post(url, json={"visible": False, "typing": False}).json()["task_id"] is None
        assert client.post(url, json={"visible": True, "typing": True}).json()["task_id"] is None
        first = client.post(
            url, json={"visible": True, "typing": False, "voice_enabled": voice_enabled}
        ).json()["task_id"]
        assert first
        with db.session.begin() as s:
            assert s.get(Task, first).payload["voice_enabled"] is voice_enabled
            assert not s.get(Task, first).payload.get("acknowledgement")
            s.get(Task, first).status = "completed"
        assert client.post(url, json={"visible": True, "typing": False}).json()["task_id"] is None
        with db.session() as s:
            assert not s.scalar(select(Message.id).where(Message.task_id == first, Message.role == "user"))


def test_resume_grace_prevents_immediate_autonomous_turn(db, config, runtime, fake):
    config.product = "roleplay"

    async def ready():
        return None

    app = create_app(config, db, fake)
    app.state.runtime.model_readiness.require = ready
    with TestClient(app) as client:
        story, actors, player = launch(client)
        client.post(
            f"/api/role/actors/{actors[0]}/memories",
            json={"kind": "goal", "content": "询问访客的决定"},
        ).raise_for_status()
        settings = f"/api/role/stories/{story}/settings"
        client.put(settings, json={"paused": True, "cooldown_seconds": 30}).raise_for_status()
        with db.session.begin() as s:
            row = s.get(Conversation, player)
            row.current_state = {**row.current_state, "last_input_at": now().timestamp() - 100}
        client.put(settings, json={"paused": False, "cooldown_seconds": 30}).raise_for_status()
        url = f"/api/role/stories/{story}/presence"
        assert client.post(url, json={"visible": True, "typing": False}).json() == {
            "task_id": None,
            "reason": "resume_cooldown",
        }
        with db.session.begin() as s:
            row = s.get(Conversation, player)
            row.current_state = {**row.current_state, "resume_grace_until": now().timestamp() - 1}
        assert client.post(url, json={"visible": True, "typing": False}).json()["task_id"]


@pytest.mark.parametrize("change", [None, "world", "node", "entry", "epoch", "legacy"])
def test_director_handoff_blocks_idle_only_for_unchanged_turn(db, config, runtime, fake, change):
    from chatbot.storage import Story

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
        client.post(
            f"/api/role/actors/{actors[0]}/memories", json={"kind": "goal", "content": "了解旅客的决定"}
        ).raise_for_status()
        runtime.submit(player, config.workspace_owner, "我还没决定", "handoff-test")
        task = runtime.claim("test")
        with db.session.begin() as s:
            story = s.get(Story, sid)
            story.settings = {**story.settings, "narrative": {"node": "n", "event_id": "entry"}}
            receipt = {"world_seq": story.seq, "node_id": "n", "entry_event_id": "entry", "epoch": task.epoch}
        result = {"incremental": True, "stop_reason": "player_turn"}
        if change != "legacy":
            result["director_handoff"] = receipt
        assert runtime.finish(task.id, task.lease_version, result)
        with db.session.begin() as s:
            c, story = s.get(Conversation, player), s.get(Story, sid)
            c.current_state = {
                **c.current_state,
                "last_input_at": now().timestamp() - 100,
                "idle_issued": False,
            }
            if change == "world":
                story.seq += 1
            elif change in {"node", "entry"}:
                field = "node" if change == "node" else "event_id"
                story.settings = {
                    **story.settings,
                    "narrative": {**story.settings["narrative"], field: "new"},
                }
            elif change == "epoch":
                c.epoch += 1
        response = client.post(
            f"/api/role/stories/{sid}/presence", json={"visible": True, "typing": False}
        ).json()
        if change is None:
            assert response == {"task_id": None, "reason": "director_handoff"}
        else:
            assert response["task_id"]


async def test_memory_reflection_validates_sources_and_actor_commitments(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    for n in range(5):
        runtime.submit(
            player,
            config.workspace_owner,
            f"我答应明天来，编号{n}",
            f"u{n}",
            channel="private",
            addressees=[actors[0]],
        )
    with db.session() as s:
        records = visible_records(s, actors[0])
    fake.replies["role_memory"] = {
        "summary": "访客承诺明天来，真假另行判断。",
        "notes": [
            {
                "kind": "belief",
                "content": "访客可能明天来",
                "source_id": records[0].id,
                "quote": "我答应明天来",
            },
            {
                "kind": "commitment",
                "content": "我答应明天来",
                "source_id": records[0].id,
                "quote": "我答应明天来",
            },
            {"kind": "goal", "content": "盗取秘密", "source_id": "forged", "quote": "秘密"},
        ],
    }
    service = RoleMemoryService(db, config, runtime, fake)
    service.schedule(actors[0], config.workspace_owner, force=True)
    task = runtime.claim("reflect", background=True)
    assert task.kind == "role_reflect"
    result = await service.reflect(task)
    assert result["notes"] == 1
    assert runtime.finish(task.id, task.lease_version, result)
    with db.session() as s:
        notes = list(s.scalars(select(RoleMemory).where(RoleMemory.kind != "episode")))
        assert len(notes) == 1 and notes[0].actor_id == actors[0] and notes[0].kind == "belief"
        assert not visible_records(s, actors[1])


async def test_redaction_removes_sources_and_prevents_snapshot_resurrection(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        for n in range(5):
            runtime.submit(
                player,
                config.workspace_owner,
                f"橙色密钥故事{n}",
                f"secret{n}",
                channel="private",
                addressees=[actors[0]],
            )
        with db.session() as s:
            record = visible_records(s, actors[0])[0]
        fake.replies["role_memory"] = {"summary": "橙色密钥故事0", "notes": []}
        memory = RoleMemoryService(db, config, runtime, fake)
        memory.schedule(actors[0], config.workspace_owner, force=True)
        task = runtime.claim("r", background=True)
        runtime.finish(task.id, task.lease_version, await memory.reflect(task))
        assert client.delete(f"/api/role/stories/{story}/events/{record.id}").status_code == 200
        task = runtime.submit(player, config.workspace_owner, "馆长继续", "after-delete")
        context, _ = memory.context(task, actors[0])
        assert "故事0" not in str(context)
        with db.session() as s:
            assert s.get(SceneRecord, record.id).redacted


async def test_invalid_claim_source_never_commits(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    fake.replies["dialogue"] = {
        "text": "我知道秘密",
        "claims": [{"text": "我知道秘密", "source_id": "another-character", "quote": "秘密"}],
    }
    runtime.submit(player, config.workspace_owner, "馆长，你知道吗", "fake-source")
    task = runtime.claim("w")
    with pytest.raises(ProviderError, match="来源"):
        await Agents(db, config, runtime, fake).respond(task)
    with db.session() as s:
        assert not s.scalar(select(Message.id).where(Message.task_id == task.id, Message.role == "assistant"))


@pytest.mark.parametrize("repairs", [True, False])
async def test_invalid_pending_reference_has_one_bounded_repair(db, config, runtime, fake, repairs):
    from chatbot.observability.timing import TimingTrace

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    calls = 0

    def plan(messages):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert '"issues": ["reply_unavailable"]' in messages[-1]["content"]
            assert "foreign-question" not in messages[-1]["content"]
        return {**intent(player), "reply_to": None if calls == 2 and repairs else "foreign-question"}

    fake.replies["role_intent"] = plan
    runtime.submit(
        player,
        config.workspace_owner,
        "馆长，聊聊吧",
        "reply-repair",
        channel="private",
        addressees=[actors[0]],
    )
    task = runtime.claim("w")
    trace = TimingTrace(task)
    with trace.activate():
        if repairs:
            await Agents(db, config, runtime, fake).respond(task)
        else:
            with pytest.raises(ProviderError, match="待回应事项"):
                await Agents(db, config, runtime, fake).respond(task)
    assert calls == 2
    stages = [s for s in trace.spans if s["name"] == "npc.intent_validation"]
    assert set(stages[0]["marks"]) == {"rejected", "reply_unavailable"}
    assert ("accepted" in stages[1]["marks"]) == repairs
    if not repairs:
        with db.session() as s:
            assert not s.scalar(
                select(Message.id).where(Message.task_id == task.id, Message.role == "assistant")
            )


@pytest.mark.parametrize("recovery", ["paraphrase", "unchanged_quote", "semantic_rejected"])
async def test_history_quote_repair_feedback_does_not_approve_the_candidate(
    db, config, runtime, fake, recovery
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    source_text = "钥匙在柜中；只是转述，尚未核实。"
    with db.session.begin() as s:
        source_id = append_record(
            s,
            story_id=story,
            kind="speech",
            actor=s.get(Conversation, player),
            text=source_text,
            observer_ids=[player, actors[0]],
            dedupe="history-source",
        ).id
    draft_text = "我保留的是“钥匙还在柜子里”这个说法，尚未核实。"
    plain_text = "我保留的是你转述的钥匙在柜中的说法，尚未核实。"
    calls = 0

    def draft(messages):
        nonlocal calls
        calls += 1
        if calls == 2:
            feedback = json.loads(
                '{"rejected_candidate"' + messages[-1]["content"].split('{"rejected_candidate"', 1)[1]
            )
            assert feedback["invalid_entries"] == [{"code": "recalled_quote_mismatch"}]
            assert feedback["invalid_history_quotes"] == ["钥匙还在柜子里"]
            assert feedback["source_texts_for_repair"] == {
                source_id: {"text": source_text, "excerpt_truncated": False}
            }
        body = draft_text if calls == 1 or recovery == "unchanged_quote" else plain_text
        return {
            "text": body,
            "claims": [{"text": body, "source_id": source_id, "quote": source_text, "mode": "reported"}],
        }

    fake.replies["dialogue"] = draft
    fake.replies["role_intent"] = intent(player)
    if recovery == "semantic_rejected":
        fake.replies["role_intent"] = lambda messages: (
            {"purpose": "回应玩家关于保留疑问的请求"}
            if messages[0]["content"].startswith("修复一名角色本次发言的目的")
            else intent(player)
        )
        fake.replies["actor_review"] = {
            "accepted": False,
            "issues": ["拒绝这个候选"],
            "checks": [
                {
                    "text": plain_text,
                    "basis": "旧发言不确认内容为真。",
                    "supported": False,
                    "reason_code": "statement_promotion",
                }
            ],
        }
    runtime.submit(
        player,
        config.workspace_owner,
        "关于这条说法，你愿意保留疑问吗？",
        "quote-feedback",
        channel="private",
        addressees=[actors[0]],
    )
    task = runtime.claim("w")
    if recovery == "paraphrase":
        await Agents(db, config, runtime, fake).respond(task)
    else:
        with pytest.raises(ProviderError):
            await Agents(db, config, runtime, fake).respond(task)
    reviews = [call for call in fake.calls if call[0] == "actor_review"]
    if recovery == "unchanged_quote":
        assert calls == 2 and not reviews
    else:
        assert reviews
    with db.session() as s:
        speeches = list(
            s.scalars(
                select(SceneRecord).where(
                    SceneRecord.task_id == task.id,
                    SceneRecord.kind == "speech",
                    SceneRecord.actor_id == actors[0],
                )
            )
        )
        assert bool(speeches) == (recovery == "paraphrase")
        if speeches:
            assert [row.text for row in speeches] == [plain_text]


async def test_citation_retry_gets_only_authorized_text_and_records_codes(db, config, runtime, fake):
    from chatbot.observability.timing import TimingTrace

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    calls = 0
    source_id = f"profile:{actors[0]}"

    def draft(messages):
        nonlocal calls
        calls += 1
        if calls == 2:
            feedback = json.loads(
                '{"rejected_candidate"' + messages[-1]["content"].split('{"rejected_candidate"', 1)[1]
            )
            assert feedback["invalid_entries"] == [{"claim_index": 0, "code": "quote_mismatch"}]
            assert set(feedback["source_texts_for_repair"]) == {source_id}
            assert "尚不知账册所在" in feedback["source_texts_for_repair"][source_id]["text"]
        return {
            "text": "我还不知道账册在哪。",
            "claims": [
                {
                    "text": "我还不知道账册在哪。",
                    "source_id": source_id,
                    "quote": "错误的引文" if calls == 1 else "尚不知账册所在",
                    "mode": "fact",
                }
            ],
        }

    fake.replies["dialogue"] = draft
    runtime.submit(
        player,
        config.workspace_owner,
        "馆长，你知道账册在哪吗",
        "repair",
        channel="private",
        addressees=[actors[0]],
    )
    task = runtime.claim("w")
    trace = TimingTrace(task)
    with trace.activate():
        await Agents(db, config, runtime, fake).respond(task)
    assert calls == 2
    stages = [row for row in trace.spans if row["name"] == "npc.claim_validation"]
    assert set(stages[0]["marks"]) == {"quote_mismatch", "rejected"}
    assert set(stages[1]["marks"]) == {"accepted"}
    assert "错误的引文" not in json.dumps(trace.spans, ensure_ascii=False)


async def test_delete_source_redacts_later_context_dependent_speech(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        for i, message in enumerate(("馆长，私下暗号是松塔", "馆长，继续聊")):
            runtime.submit(
                player,
                config.workspace_owner,
                message,
                f"dependent-{i}",
                channel="private",
                addressees=[actors[0]],
            )
            task = runtime.claim("w")
            runtime.finish(task.id, task.lease_version, await Agents(db, config, runtime, fake).respond(task))
        with db.session() as s:
            source = visible_records(s, actors[0])[0]
            later = list(
                s.scalars(
                    select(SceneRecord).where(
                        SceneRecord.task_id == task.id, SceneRecord.actor_id == actors[0]
                    )
                )
            )
            assert source.id in later[0].payload["context_source_ids"]
        assert client.delete(f"/api/role/stories/{story}/events/{source.id}").status_code == 200
        with db.session() as s:
            assert s.get(SceneRecord, later[0].id).redacted
            assert all(m.redacted for m in s.scalars(select(Message).where(Message.task_id == task.id)))


def test_location_and_cross_story_permissions(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        other, others, _ = launch(client)
        assert (
            client.patch(f"/api/role/actors/{actors[1]}/location", json={"location": "库房"}).status_code
            == 200
        )
        runtime.submit(player, config.workspace_owner, "在大厅说话", "hall")
        observed = client.get(f"/api/role/stories/{story}/timeline", params={"actor_id": actors[1]}).json()[
            "events"
        ]
        assert [e["kind"] for e in observed] == ["scene_change"]
        assert (
            client.get(f"/api/role/stories/{story}/timeline", params={"actor_id": others[0]}).status_code
            == 400
        )
        with pytest.raises(ValueError):
            runtime.submit(
                player,
                config.workspace_owner,
                "私聊库房",
                "outside",
                channel="private",
                addressees=[actors[1]],
            )
    config.product = "office"
    with TestClient(create_app(config, db, fake)) as client:
        assert client.get(f"/api/role/stories/{other}/timeline").status_code == 404
