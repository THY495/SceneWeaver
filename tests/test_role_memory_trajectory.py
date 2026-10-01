import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention, launch

from chatbot.api import create_app
from chatbot.event_projection import projected_exchanges
from chatbot.observations import append_record, visible_records
from chatbot.role_memory import RoleMemoryService
from chatbot.runtime import Runtime
from chatbot.storage import Conversation, RoleMemory, RoleSnapshot, Task
from chatbot.worker import Worker

CASE = json.loads(
    (Path(__file__).parents[1] / "evals/cases/role-memory-trajectory-v1.json").read_text(
        encoding="utf-8"
    )
)


def add_event(db, story, actor_id, observer_ids, index, text, *, perceptions=None):
    with db.session.begin() as s:
        actor = s.get(Conversation, actor_id)
        return append_record(
            s,
            story_id=story,
            kind="speech",
            text=text,
            actor=actor,
            observer_ids=observer_ids,
            perceptions=perceptions,
            dedupe=f"trajectory-{index}",
        ).id


async def reflect_once(service, runtime, actor_id, owner, summary, fake):
    fake.replies["role_memory"] = {"summary": summary, "notes": []}
    scheduled = service.schedule(actor_id, owner, force=True)
    assert scheduled
    task = runtime.claim(f"reflect-{scheduled.id}", background=True)
    result = await service.reflect(task)
    assert runtime.finish(task.id, task.lease_version, result)
    return result


async def test_fixed_role_memory_trajectory_survives_two_snapshots_redaction_and_restart(
    db, config, runtime, fake
):
    assert CASE["version"] == 1 and len(CASE["checkpoints"]) == 3
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        keeper, listener, absent = actors
        goal_id = client.post(
            f"/api/role/actors/{keeper}/memories",
            json={"kind": "commitment", "content": "我答应归还钟摆"},
        ).json()["id"]

        secret = add_event(
            db,
            story,
            player,
            [player, keeper, listener],
            1,
            "管理员转述：银钥匙在钟室抽屉，抽屉密码是四一七。",
            perceptions={
                listener: {"text": "银钥匙在钟室抽屉", "speaker_known": False},
            },
        )
        for index in range(2, 6):
            add_event(db, story, keeper, [keeper], index, f"守钟记录第{index}项。")

        with db.session() as s:
            keeper_records = visible_records(s, keeper)
            listener_records = visible_records(s, listener)
            assert any("密码是四一七" in row.text for row in keeper_records)
            assert len(listener_records) == 1
            assert listener_records[0].text == "银钥匙在钟室抽屉"
            assert listener_records[0].actor_id is None
            assert visible_records(s, absent) == []

        service = RoleMemoryService(db, config, runtime, fake)
        first = await reflect_once(
            service,
            runtime,
            keeper,
            config.workspace_owner,
            "玩家转述银钥匙在钟室抽屉；来源尚未确认。",
            fake,
        )
        with db.session() as s:
            first_snapshot = s.scalar(
                select(RoleSnapshot).where(RoleSnapshot.actor_id == keeper)
            )
            assert first["cut"] == first_snapshot.covered_seq
            assert len(first_snapshot.source_ids) == len(visible_records(s, keeper)) - 2

        correction = add_event(
            db,
            story,
            player,
            [player, keeper],
            6,
            "我纠正：银钥匙不在钟室抽屉，而在档案柜；先前只是管理员转述。",
        )
        for index in range(7, 11):
            add_event(db, story, keeper, [keeper], index, f"纠正后的守钟记录第{index}项。")
        second = await reflect_once(
            service,
            runtime,
            keeper,
            config.workspace_owner,
            "原说法是银钥匙在钟室抽屉，后来纠正为档案柜；原说法只是转述。",
            fake,
        )
        assert second["cut"] > first["cut"]

        probe = runtime.submit(
            player,
            config.workspace_owner,
            "我对银钥匙的最初说法和后来纠正分别是什么？",
            "trajectory-probe-before-redaction",
            channel="private",
            addressees=[keeper],
        )
        messages, workset = service.context(probe, keeper)
        encoded = json.dumps(messages, ensure_ascii=False)
        assert workset["snapshot_id"]
        assert "档案柜" in encoded and "我答应归还钟摆" in encoded
        assert goal_id in workset["active_goals"]
        with db.session() as s:
            snapshots = list(
                s.scalars(
                    select(RoleSnapshot)
                    .where(RoleSnapshot.actor_id == keeper)
                    .order_by(RoleSnapshot.covered_seq)
                )
            )
            assert len(snapshots) == 2
            assert set(snapshots[0].source_ids).issubset(snapshots[1].source_ids)
            old_privacy = s.get(Conversation, keeper).privacy_version

        assert client.delete(f"/api/role/stories/{story}/events/{secret}").status_code == 200
        restarted_runtime = Runtime(db, config)
        restarted = RoleMemoryService(db, config, restarted_runtime, fake)
        after = restarted_runtime.submit(
            player,
            config.workspace_owner,
            "撤回来源后，只按当前仍有效的记录回答。",
            "trajectory-probe-after-redaction",
            channel="private",
            addressees=[keeper],
        )
        messages, workset = restarted.context(after, keeper)
        encoded = json.dumps(messages, ensure_ascii=False)
        assert workset["snapshot_id"] is None
        assert "密码是四一七" not in encoded
        assert "银钥匙不在钟室抽屉，而在档案柜" in encoded
        assert "我答应归还钟摆" in encoded and goal_id in workset["active_goals"]
        with db.session() as s:
            actor = s.get(Conversation, keeper)
            assert actor.privacy_version == old_privacy + 1
            assert s.get(RoleMemory, goal_id).status == "active"
            assert correction in {row.id for row in visible_records(s, keeper)}


async def test_relationship_change_and_assigned_npc_question_cross_two_snapshots(
    db, config, runtime, fake
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = launch(client)
        asker, responder = actors[:2]
        base = f"/api/role/stories/{story}"
        assert client.put(base + "/settings", json={"paused": True}).status_code == 200

        revision = next(
            person["revision"]
            for person in client.get(base + "/roster").json()
            if person["id"] == responder
        )
        relationship = {
            "expected_revision": revision,
            "target": "馆长",
            "relation": "对交接方式存在分歧",
            "status": "current",
            "basis": "此前一次口头约定被误解",
        }
        first_relation = client.post(
            f"/api/role/actors/{responder}/relationships", json=relationship
        )
        assert first_relation.status_code == 201
        for index in range(4):
            response = client.post(
                base + "/speech-events",
                json={
                    "request_id": f"before-relation-{index}",
                    "actor_id": responder,
                    "text": f"交接记录检查第{index + 1}项已登记。",
                },
            )
            assert response.status_code == 201, response.text
        fake.replies["role_memory"] = {
            "summary": "修复师与馆长对交接方式存在分歧，原因是口头约定曾被误解。",
            "notes": [],
        }
        first = client.post(
            base + "/memory-snapshots",
            json={"request_id": "relationship-first", "actor_id": responder},
        ).json()
        task = runtime.claim("relationship-first", background=True)
        await Worker(db, config, fake).run_task(task)
        assert client.get(base + f"/memory-snapshots/{first['task_id']}").json()["status"] == "completed"

        revision = next(
            person["revision"]
            for person in client.get(base + "/roster").json()
            if person["id"] == responder
        )
        relationship.update(
            expected_revision=revision,
            relation="恢复谨慎合作",
            basis="双方同意先核对书面记录",
        )
        updated_relation = client.post(
            f"/api/role/actors/{responder}/relationships", json=relationship
        )
        assert updated_relation.status_code == 201
        updated_relation_id = updated_relation.json()["id"]
        question = client.post(
            base + "/speech-events",
            json={
                "request_id": "assigned-after-relation",
                "actor_id": asker,
                "text": "修复师，你是否同意按书面记录继续交接？",
                "observations": [{"actor_id": responder}],
                "expects_reply": True,
                "target_id": responder,
            },
        )
        assert question.status_code == 201, question.text
        question_id = question.json()["id"]
        for index in range(3):
            response = client.post(
                base + "/speech-events",
                json={
                    "request_id": f"after-relation-{index}",
                    "actor_id": responder,
                    "text": f"更新后的检查记录第{index + 1}项。",
                },
            )
            assert response.status_code == 201, response.text
        fake.replies["role_memory"] = {
            "summary": "修复师与馆长恢复谨慎合作；馆长明确询问是否按书面记录继续交接。",
            "notes": [
                {
                    "kind": "relationship",
                    "content": "与馆长恢复谨慎合作",
                    "source_id": updated_relation_id,
                    "quote": "恢复谨慎合作",
                }
            ],
        }
        second = client.post(
            base + "/memory-snapshots",
            json={"request_id": "relationship-second", "actor_id": responder},
        ).json()
        task = runtime.claim("relationship-second", background=True)
        await Worker(db, config, fake).run_task(task)
        second_state = client.get(base + f"/memory-snapshots/{second['task_id']}").json()
        assert second_state["status"] == "completed" and second_state["notes"] == 0

        with db.session.begin() as s:
            deferred = append_record(
                s,
                story_id=story,
                actor=s.get(Conversation, responder),
                kind="speech",
                text="我需要再核对一下，稍后明确答复。",
                observer_ids=[asker, responder],
                dedupe="defer-assigned-question",
                payload={"exchange": {"reply_to": question_id, "response_kind": "defer"}},
            )
            assert deferred.id
            request = next(
                item for item in projected_exchanges(s, responder) if item["question_id"] == question_id
            )
            assert request["status"] == "defer"

        fake.replies["role_intent"] = intention(
            asker,
            reply_to=question_id,
            response_kind="answer",
            expects_reply=False,
            wait_for_player=False,
        )
        step = client.post(
            base + "/steps",
            json={"request_id": "answer-assigned-question", "actor_id": responder},
        ).json()
        with db.session() as s:
            assert s.get(Task, step["task_id"]).payload["resume_event_id"] == question_id
        client.post(base + f"/steps/{step['task_id']}/presence", json={"visible": True})
        task = runtime.claim("assigned-answer", background=True)
        await Worker(db, config, fake).run_task(task)
        result = client.get(base + f"/steps/{step['task_id']}").json()
        assert result["status"] == "completed" and result["outcome"] == "committed", result
        assert len(result["events"]) == 1 and result["events"][0]["actor_id"] == responder

        with db.session() as s:
            snapshots = list(s.scalars(select(RoleSnapshot).where(RoleSnapshot.actor_id == responder)))
            assert len(snapshots) == 2
            exchanges = projected_exchanges(s, responder)
            request = next(item for item in exchanges if item["question_id"] == question_id)
            assert request["status"] == "answer"
            profile = s.get(Conversation, responder).current_state["profile"]
            relation = next(item for item in profile["relationships"] if item["target"] == "馆长")
            assert relation["relation"] == "恢复谨慎合作"
            relation_memories = list(
                s.scalars(
                    select(RoleMemory)
                    .where(RoleMemory.actor_id == responder, RoleMemory.kind == "relationship")
                    .order_by(RoleMemory.created_at)
                )
            )
            active = [memory for memory in relation_memories if memory.status == "active"]
            archived = [memory for memory in relation_memories if memory.status == "archived"]
            assert len(active) == 1 and "恢复谨慎合作" in active[0].content
            assert len(archived) == 1 and "存在分歧" in archived[0].content
            assert active[0].replaces_id == archived[0].id
