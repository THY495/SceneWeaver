"""A pending private reflection must not become a foreground turn dependency."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention, launch

from chatbot.api import create_app
from chatbot.observations import append_record, visible_records
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, ExperimentInitial, RoleSnapshot, SceneRecord, Story, Task
from chatbot.worker import Worker


def add_speech(db, story, speaker, observers, key, text, *, perceptions=None):
    with db.session.begin() as session:
        return append_record(
            session, story_id=story, kind="speech", text=text,
            actor=session.get(Conversation, speaker), observer_ids=observers,
            perceptions=perceptions, dedupe=key,
        ).id


def summary(messages):
    data = json.loads(messages[-1]["content"])
    # A deliberately transparent test summary keeps the actual heard quotes,
    # not a hand-written summary which could accidentally bypass isolation.
    return {"summary": json.dumps({"previous": data["previous"],
            "statements": data["observations"]}, ensure_ascii=False), "notes": []}


@asynccontextmanager
async def memory_case(db, config, runtime, fake):
    config.product = "roleplay"
    config.role_actor_generation = "combined_context"
    fake.replies["role_memory"] = summary
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        keeper, listener, absent = actors
        with db.session.begin() as session:
            current = session.get(Story, story)
            current.settings = {**current.settings,
                "narrative": {"node": "room", "event_id": "entry"}}
            session.add(ExperimentInitial(
                story_id=story, owner_id=config.workspace_owner,
                request_id="memory-concurrency", request_hash="memory-concurrency",
                receipt={"actors": {"keeper": keeper, "listener": listener, "absent": absent}},
                recipe={"people": [], "narrative": {"edges": [], "nodes": [{
                    "id": "room", "direction": {"objective": "核对玩家的说法", "beats": [{
                        "id": "memory", "title": "核对", "objective": "回应玩家最新说法",
                        "participants": ["keeper"], "completion": "玩家决定下一步", "max_segments": 1,
                    }]},
                }]}},
            ))
        secret = add_speech(db, story, player, [player, keeper, listener], "secret",
            "管理员转述：银钥匙在钟室抽屉，抽屉密码是四一七。",
            perceptions={listener: {"text": "银钥匙在钟室抽屉", "speaker_known": False}})
        for index in range(4):
            add_speech(db, story, keeper, [keeper], f"before-{index}", f"馆长登记旧记录{index}。")
        service = RoleMemoryService(db, config, runtime, fake)
        first = service.schedule(keeper, config.workspace_owner, force=True)
        initial_task = runtime.claim("initial-reflection", background=True)
        assert initial_task.id == first.id
        result = await service.reflect(initial_task)
        assert runtime.finish(initial_task.id, initial_task.lease_version, result)
        with db.session() as session:
            baseline = session.scalar(select(RoleSnapshot).where(RoleSnapshot.actor_id == keeper))
            assert secret in baseline.source_ids
        for index in range(4):
            add_speech(db, story, keeper, [keeper], f"during-{index}", f"馆长登记后续记录{index}。")

        scheduled = service.schedule(keeper, config.workspace_owner, force=True)
        entered, release = asyncio.Event(), asyncio.Event()
        original_chat = fake.chat

        async def held_chat(task_id, role, messages, **kwargs):
            value = await original_chat(task_id, role, messages, **kwargs)
            if task_id == scheduled.id and role == "role_memory":
                entered.set()
                await release.wait()
            return value

        fake.chat = held_chat
        fake.calls.clear()
        worker = Worker(db, config, fake)
        lane = asyncio.create_task(worker.lane(config.worker_concurrency - 1, background=True))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            yield {
                "client": client, "story": story, "keeper": keeper, "listener": listener,
                "absent": absent, "player": player, "secret": secret, "baseline": baseline,
                "reflection": scheduled, "service": service, "release": release,
                "worker": worker, "lane": lane,
            }
        finally:
            release.set()
            worker.stop.set()
            lane.cancel()
            await asyncio.gather(lane, return_exceptions=True)
            fake.chat = original_chat


async def task_terminal(db, task_id):
    # Poll a known Task, not a log file or elapsed-time assumption. The timeout
    # is a test deadlock guard, not a latency assertion.
    async def poll():
        while True:
            with db.session() as session:
                task = session.get(Task, task_id)
                if task.status in {"completed", "failed", "cancelled", "superseded"}:
                    return task
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), 5)


def context_data(service, task, actor):
    messages, info = service.context(task, actor)
    return json.dumps(messages, ensure_ascii=False), info


def configure_directed_turn(fake, keeper, player):
    fake.replies["director"] = {
        "beat_id": "memory", "jobs": [{"kind": "free", "id": "respond", "actor": keeper,
            "addressees": [player], "objective": "回应访客最新说法，区分转述与确认"}],
        "after_plan": "yield_player", "remaining_work": [], "reason": "核对本次纠正",
    }
    fake.replies["director_assignment"] = {"checks": [], "accepted": True, "issues": []}
    fake.replies["director_progress"] = {"status": "not_met", "evidence": [], "reason": "玩家尚未决定"}


@pytest.mark.parametrize("lane_count", [2, 4])
async def test_foreground_worker_commits_combined_turn_while_reflection_waits(
    db, config, runtime, fake, lane_count,
):
    config.worker_concurrency = lane_count
    async with memory_case(db, config, runtime, fake) as case:
        keeper, player = case["keeper"], case["player"]
        correction = "我纠正：银钥匙在档案柜；先前只是管理员转述，尚未核实。"
        submitted = runtime.submit(player, config.workspace_owner, correction, "hot-correction",
            channel="private", addressees=[keeper])
        source = submitted.payload["event_id"]
        before, info = context_data(case["service"], submitted, keeper)
        assert info["snapshot_id"] == case["baseline"].id
        assert source in {event["id"] for event in info["observations"]}
        assert correction in before
        configure_directed_turn(fake, keeper, player)
        answer = "你刚说银钥匙在档案柜，先前只是转述，还没核实。"

        def draft(messages):
            assert correction in json.dumps(messages, ensure_ascii=False)
            return {"intent": intention(player, source_ids=[source]),
                "speech": {"text": answer, "spoken_parts": [answer], "claims": []}}

        fake.replies["dialogue"] = draft
        foreground = asyncio.create_task(case["worker"].lane(0, background=False))
        try:
            finished = await task_terminal(db, submitted.id)
            assert finished.status == "completed", finished.result
            assert finished.result["answer"] == answer
            with db.session() as session:
                pending = session.get(Task, case["reflection"].id)
                assert pending.status == "running" and not case["release"].is_set()
                records = list(session.scalars(select(SceneRecord).where(
                    SceneRecord.task_id == submitted.id, SceneRecord.actor_id == keeper)))
                assert len(records) == 1 and records[0].text == answer
                answer_id = records[0].id
            turn_calls = [role for role, _, _ in fake.calls if role != "role_memory"]
            assert turn_calls.count("dialogue") == 1 and turn_calls.count("actor_review") == 1
            assert "role_intent" not in turn_calls

            case["release"].set()
            reflected = await task_terminal(db, case["reflection"].id)
            assert reflected.status == "completed" and not reflected.result.get("stale")
            after, info = context_data(case["service"], submitted, keeper)
            assert info["snapshot_id"] != case["baseline"].id
            assert correction in after and answer in after
            assert {source, answer_id} <= {event["id"] for event in info["observations"]}
            with db.session() as session:
                snapshot = session.get(RoleSnapshot, info["snapshot_id"])
                assert snapshot.covered_seq == case["reflection"].payload["cut"]
                assert source not in snapshot.source_ids and answer_id not in snapshot.source_ids
                assert snapshot.covered_seq < session.get(SceneRecord, source).seq

            listener_text, listener_info = context_data(case["service"], submitted, case["listener"])
            absent_text, absent_info = context_data(case["service"], submitted, case["absent"])
            assert "四一七" not in listener_text and correction not in listener_text and answer not in listener_text
            heard = next(event for event in listener_info["observations"] if event["id"] == case["secret"])
            assert heard["text"] == "银钥匙在钟室抽屉" and heard["actor_id"] is None
            assert case["secret"] not in {event["id"] for event in absent_info["observations"]}
            assert "四一七" not in absent_text and "银钥匙在钟室抽屉" not in absent_text

            # Reflect the partial listener through the same running background
            # lane: its private summary must use its excerpt, not the shared row.
            for index in range(4):
                add_speech(db, case["story"], case["listener"], [case["listener"]],
                    f"listener-{index}", f"修复师整理自己的记录{index}。")
            listener_reflection = case["service"].schedule(case["listener"], config.workspace_owner, force=True)
            listener_done = await task_terminal(db, listener_reflection.id)
            assert listener_done.status == "completed" and not listener_done.result.get("stale")
            listener_text, listener_info = context_data(case["service"], submitted, case["listener"])
            assert listener_info["snapshot_id"] and listener_info["snapshot_id"] != info["snapshot_id"]
            assert "四一七" not in listener_text and correction not in listener_text and answer not in listener_text
            with db.session() as session:
                listener_snapshot = session.get(RoleSnapshot, listener_info["snapshot_id"])
                assert case["secret"] in listener_snapshot.source_ids
                statement = next(item for item in json.loads(listener_snapshot.summary["summary"])["statements"]
                    if item["id"] == case["secret"])
                assert statement["text"] == "银钥匙在钟室抽屉" and statement["actor_id"] is None
        finally:
            case["worker"].stop.set()
            foreground.cancel()
            await asyncio.gather(foreground, return_exceptions=True)


async def test_redaction_during_pending_reflection_cannot_install_old_private_summary(
    db, config, runtime, fake,
):
    async with memory_case(db, config, runtime, fake) as case:
        probe = runtime.submit(case["player"], config.workspace_owner, "按现有记录核对。", "redaction-probe",
            channel="private", addressees=[case["keeper"]])
        before, info = context_data(case["service"], probe, case["keeper"])
        assert info["snapshot_id"] == case["baseline"].id and "四一七" in before
        with db.session() as session:
            privacy = session.get(Conversation, case["keeper"]).privacy_version
        response = case["client"].delete(f"/api/role/stories/{case['story']}/events/{case['secret']}")
        assert response.status_code == 200, response.text
        case["release"].set()
        finished = await task_terminal(db, case["reflection"].id)
        assert finished.status == "completed" and finished.result == {"stale": True}
        with db.session() as session:
            assert session.get(Conversation, case["keeper"]).privacy_version == privacy + 1
            assert len(list(session.scalars(select(RoleSnapshot).where(
                RoleSnapshot.actor_id == case["keeper"])))) == 1
            for actor in (case["keeper"], case["listener"], case["absent"]):
                assert case["secret"] not in {event.id for event in visible_records(session, actor)}
        restarted = RoleMemoryService(db, config, runtime, fake)
        for actor in (case["keeper"], case["listener"], case["absent"]):
            after, info = context_data(restarted, probe, actor)
            assert info["snapshot_id"] is None and "四一七" not in after
            assert "银钥匙在钟室抽屉" not in after


async def test_snapshot_switch_during_joint_generation_invalidates_private_draft(
    db, config, runtime, fake,
):
    async with memory_case(db, config, runtime, fake) as case:
        keeper, player = case["keeper"], case["player"]
        correction = "我纠正：银钥匙在档案柜；先前的转述尚未核实。"
        submitted = runtime.submit(player, config.workspace_owner, correction, "snapshot-draft-race",
            channel="private", addressees=[keeper])
        source = submitted.payload["event_id"]
        configure_directed_turn(fake, keeper, player)
        drafting, release_draft = asyncio.Event(), asyncio.Event()
        original_chat = fake.chat
        old_text, new_text = "我先记下你刚才的说法。", "你改口说银钥匙在档案柜，这仍是待核实的转述。"
        generations = 0
        generation_worksets = []

        def draft(messages):
            nonlocal generations
            generations += 1
            assert correction in json.dumps(messages, ensure_ascii=False)
            generation_worksets.append(json.loads(messages[1]["content"].partition("：")[2]))
            speech = {"text": old_text if generations == 1 else new_text,
                "spoken_parts": [old_text if generations == 1 else new_text], "claims": []}
            return {"intent": intention(player, source_ids=[source]), "speech": speech} if generations == 1 else speech

        async def hold_first_draft(task_id, role, messages, **kwargs):
            value = await original_chat(task_id, role, messages, **kwargs)
            if task_id == submitted.id and role == "dialogue" and not drafting.is_set():
                drafting.set()
                await release_draft.wait()
            return value

        fake.replies["dialogue"] = draft
        fake.chat = hold_first_draft
        foreground = asyncio.create_task(case["worker"].lane(0, background=False))
        try:
            await asyncio.wait_for(drafting.wait(), 5)
            case["release"].set()
            reflected = await task_terminal(db, case["reflection"].id)
            assert reflected.status == "completed" and not reflected.result.get("stale")
            _, info = context_data(case["service"], submitted, keeper)
            assert info["snapshot_id"] != case["baseline"].id
            with db.session() as session:
                switched_summary = session.get(RoleSnapshot, info["snapshot_id"]).summary
            release_draft.set()
            finished = await task_terminal(db, submitted.id)
            assert finished.status == "completed", finished.result
            assert finished.result["answer"] == new_text and old_text not in finished.result["answer"]
            assert generations == 2
            assert generation_worksets[0]["historical_summary"] == case["baseline"].summary
            assert generation_worksets[1]["historical_summary"] == switched_summary
            assert "馆长登记后续记录0。" not in generation_worksets[0]["historical_summary"]["summary"]
            assert "馆长登记后续记录0。" in generation_worksets[1]["historical_summary"]["summary"]
            calls = [role for role, _, _ in fake.calls if role != "role_memory"]
            assert calls.count("dialogue") == 2 and calls.count("actor_review") == 1
            assert "role_intent" not in calls
            with db.session() as session:
                committed = list(session.scalars(select(SceneRecord).where(
                    SceneRecord.task_id == submitted.id, SceneRecord.actor_id == keeper)))
                assert len(committed) == 1 and committed[0].text == new_text
        finally:
            release_draft.set()
            case["worker"].stop.set()
            foreground.cancel()
            await asyncio.gather(foreground, return_exceptions=True)
            fake.chat = original_chat
