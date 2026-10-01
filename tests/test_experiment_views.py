from fastapi.testclient import TestClient
from support.story import launch

from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, Operation, RoleMemory, RoleSnapshot, uid


def test_observation_frame_filters_hearing_memories_and_foreign_actor(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
        base = f"/api/role/stories/{sid}"
        event = client.post(
            base + "/speech-events",
            json={
                "request_id": "frame",
                "actor_id": actors[0],
                "text": "盒子在楼上，钥匙在暗格。",
                "observations": [{"actor_id": actors[1], "text": "盒子在楼上", "speaker_known": False}],
            },
        )
        assert event.status_code == 201, event.text
        eid = event.json()["id"]
        with db.session.begin() as s:
            listener_actor = s.get(Conversation, actors[1])
            s.add(
                RoleSnapshot(
                    id=uid(),
                    actor_id=actors[1],
                    covered_seq=1,
                    privacy_version=listener_actor.privacy_version,
                    summary={"summary": "只听到盒子在楼上，来源身份未知。"},
                    source_ids=[eid],
                )
            )
            hidden_actor = s.get(Conversation, actors[2])
            s.add(
                RoleSnapshot(
                    id=uid(),
                    actor_id=actors[2],
                    covered_seq=1,
                    privacy_version=hidden_actor.privacy_version,
                    summary={"summary": "不应泄露的快照"},
                    source_ids=[eid],
                )
            )
        path = base + "/observation-frame"
        creator = client.get(path).json()
        listener = client.get(path, params={"actor_id": actors[1]}).json()
        assert creator["events"][-1]["text"] == "盒子在楼上，钥匙在暗格。"
        assert listener["events"][-1]["text"] == "盒子在楼上"
        assert listener["events"][-1]["speaker"] == "未辨认的声音"
        assert "钥匙" not in str(listener) and "馆长" not in str(listener)
        assert listener["memory_total"] == 1 and listener["memories"][0]["source_ids"] == [eid]
        assert listener["snapshot"]["source_ids"] == [eid]
        assert listener["snapshot"]["summary"] == "只听到盒子在楼上，来源身份未知。"
        for aid in (actors[2], player):
            frame = client.get(path, params={"actor_id": aid}).json()
            assert frame["events"] == [] and frame["memories"] == [] and frame["snapshot"] is None
        other, others, _ = launch(client)
        assert client.get(path, params={"actor_id": others[0]}).status_code == 404
        assert client.get("/api/role/stories/missing/observation-frame").status_code == 404
        # A stale derived memory with an inaccessible source cannot leak through the new endpoint.
        with db.session.begin() as s:
            s.add(
                RoleMemory(
                    story_id=sid, actor_id=actors[2], kind="belief", content="隐藏来源内容", source_ids=[eid]
                )
            )
        assert client.get(path, params={"actor_id": actors[2]}).json()["memories"] == []
        assert client.delete(base + f"/events/{eid}").status_code == 200
        deleted = client.get(path, params={"actor_id": actors[1]}).json()
        assert deleted["memories"] == [] and deleted["snapshot"] is None


def test_observation_frame_bounds_and_memory_updates(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, _ = launch(client)
        path = f"/api/role/stories/{sid}/observation-frame"
        with db.session.begin() as s:
            for number in range(103):
                s.add(
                    RoleMemory(
                        story_id=sid,
                        actor_id=actors[0],
                        kind="episode",
                        content=f"历史条目{number}",
                        source_ids=[],
                    )
                )
        frame = client.get(path, params={"actor_id": actors[0]}).json()
        assert frame["memory_total"] == 103 and len(frame["memories"]) == 100
        mid = client.post(
            f"/api/role/actors/{actors[0]}/memories", json={"kind": "goal", "content": "查清来历"}
        ).json()["id"]
        before = client.get(path, params={"actor_id": actors[0]}).json()
        assert (
            client.patch(
                f"/api/role/actors/{actors[0]}/memories/{mid}",
                json={"expected_version": 1, "status": "completed"},
            ).status_code
            == 200
        )
        after = client.get(path, params={"actor_id": actors[0]}).json()
        assert after["story_seq"] > before["story_seq"]
        memory = next(m for m in after["memories"] if m["id"] == mid)
        assert memory["version"] == 2 and memory["status"] == "completed"
        # Reading a frame never changes the world or the actor revision.
        repeated = client.get(path, params={"actor_id": actors[0]}).json()
        assert repeated["actor"] == after["actor"] and repeated["story_seq"] == after["story_seq"]
    config.product = "office"
    with TestClient(create_app(config, db, fake)) as client:
        assert client.get(path).status_code == 404


def test_observation_frame_shows_deferred_reply_memory_chain_and_actual_snapshot_use(
    db, config, runtime, fake
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        target = actors[0]
        task = runtime.submit(
            player,
            config.workspace_owner,
            "你核对完第六项了吗？",
            "observable-defer",
            addressees=[target],
        )
        question_id = task.payload["event_id"]
        with db.session.begin() as s:
            npc = s.get(Conversation, target)
            deferred = append_record(
                s,
                story_id=story,
                actor=npc,
                kind="speech",
                text="我需要再核对一下，稍后回答。",
                observer_ids=[player, target],
                dedupe="observable-defer-reply",
                payload={"exchange": {"reply_to": question_id, "response_kind": "defer"}},
            )
            old = RoleMemory(
                story_id=story,
                actor_id=target,
                kind="relationship",
                content="对玩家保持戒备",
                status="archived",
                version=2,
                source_ids=[question_id],
            )
            s.add(old)
            s.flush()
            current = RoleMemory(
                story_id=story,
                actor_id=target,
                kind="relationship",
                content="愿意谨慎合作",
                replaces_id=old.id,
                source_ids=[deferred.id],
            )
            s.add(current)
            snapshot = RoleSnapshot(
                id=uid(),
                actor_id=target,
                covered_seq=deferred.seq,
                privacy_version=npc.privacy_version,
                summary={"summary": "已暂缓回答第六项。"},
                source_ids=[question_id, deferred.id],
            )
            s.add(snapshot)
            s.add(
                Operation(
                    id=f"timing:{task.id}:observable",
                    task_id=task.id,
                    payload={
                        "timing_v1": {
                            "worksets": [
                                {
                                    "actor_id": target,
                                    "revision": npc.revision,
                                    "privacy_version": npc.privacy_version,
                                    "snapshot_id": snapshot.id,
                                    "estimated_tokens": 321,
                                    "observation_ids": [question_id, deferred.id],
                                    "memory_ids": [current.id],
                                    "omitted_id_count": 0,
                                    "scope": "assembled_role_context_before_stage_prompt",
                                }
                            ]
                        }
                    },
                )
            )
        _, info = RoleMemoryService(db, config, runtime, fake).context(task, target)
        assert info["pending_replies"][0]["status"] == "defer"
        frame = client.get(
            f"/api/role/stories/{story}/observation-frame", params={"actor_id": target}
        ).json()
        assert frame["exchanges"][0]["status"] == "defer"
        assert [item["status"] for item in frame["exchanges"][0]["transitions"]] == [
            "open",
            "defer",
        ]
        assert frame["latest_context_use"]["snapshot_id"] == snapshot.id
        assert frame["latest_context_use"]["estimated_tokens"] == 321
        new_memory = next(item for item in frame["memories"] if item["id"] == current.id)
        assert new_memory["replaces_id"] == old.id
