from fastapi.testclient import TestClient
from sqlalchemy import func, select

from chatbot.api import create_app
from chatbot.storage import (
    Conversation,
    Operation,
    RoleMemory,
    RoleSnapshot,
    SceneRecord,
    Story,
    Task,
    Usage,
    uid,
)


def setup(client, request_id="archive"):
    response = client.post(
        "/api/role/experiments/start", json={"request_id": request_id, "template_id": "partial-hearing"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_run_record_scope_redaction_and_read_only(db, config, fake, runtime):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = setup(client)
        sid = run["story_id"]
        base = "/api/role/experiments"
        with db.session.begin() as s:
            initial_task_count = s.scalar(select(func.count()).select_from(Task))
            task = runtime.enqueue(
                s,
                "npc_step",
                config.workspace_owner,
                {"story_id": sid, "text": "DO_NOT_EXPORT_RAW", "step_actor_id": run["actors"]["restorer"]},
            )
            task.status = "failed"
            task.error = "DO_NOT_EXPORT_ERROR"
            child = runtime.enqueue(s, "reflect", config.workspace_owner, {}, parent_id=task.id)
            child.status = "cancelled"
            s.add(
                Usage(
                    owner_id=config.workspace_owner,
                    task_id=task.id,
                    role="dialogue",
                    model="test",
                    status="failed",
                    cost=0.12,
                    reserved=0.03,
                )
            )
            s.add(
                Usage(
                    owner_id=config.workspace_owner,
                    task_id=child.id,
                    role="memory",
                    model="test",
                    status="completed",
                    cost=0.04,
                )
            )
            s.add(
                Operation(
                    id=f"timing:{task.id}:test",
                    task_id=task.id,
                    payload={
                        "timing_v1": {
                            "duration_ms": 300,
                            "attempt": 1,
                            "worksets": [{"secret": "DO_NOT_EXPORT_RAW"}],
                        }
                    },
                )
            )
            # Invalid derived source must not survive through a creator export.
            s.add(
                RoleMemory(
                    story_id=sid,
                    actor_id=run["player_id"],
                    kind="belief",
                    content="DO_NOT_EXPORT_MEMORY",
                    source_ids=["unavailable"],
                )
            )
            source = s.scalar(
                select(SceneRecord).where(SceneRecord.story_id == sid, SceneRecord.kind == "speech")
            )
            actor = s.get(Conversation, run["actors"]["restorer"])
            previous_memory = RoleMemory(
                id=uid(),
                story_id=sid,
                actor_id=actor.id,
                kind="belief",
                content="有人提到盒子在楼上",
                source_ids=[source.id],
                status="archived",
            )
            s.add(previous_memory)
            s.add(
                RoleMemory(
                    id=uid(),
                    story_id=sid,
                    actor_id=actor.id,
                    kind="belief",
                    content="盒子位置仍未确认",
                    source_ids=[source.id],
                    replaces_id=previous_memory.id,
                )
            )
            s.add(
                RoleSnapshot(
                    id=uid(),
                    actor_id=actor.id,
                    covered_seq=source.seq,
                    privacy_version=actor.privacy_version,
                    summary={"summary": "快照中的匿名听闻"},
                    source_ids=[source.id],
                )
            )
            original_seq = s.get(Story, sid).seq
        record = client.get(base + f"/runs/{sid}/record").json()
        assert "DO_NOT_EXPORT" not in str(record)
        assert record["cost"]["settled_usd"] == 0.16 and record["cost"]["reserved_usd"] == 0.03
        assert {t["status"] for t in record["tasks"]} == {"failed", "cancelled"}
        speech = next(e for e in record["events"] if e["kind"] == "speech")
        view = next(
            o["view"] for o in speech["observations"] if o["observer_id"] == run["actors"]["restorer"]
        )
        assert view["text"] == "盒子在楼上" and view["speaker"] == "未辨认的声音"
        assert record["snapshots"][0]["summary"] == "快照中的匿名听闻"
        active_memory = next(m for m in record["memories"] if m["content"] == "盒子位置仍未确认")
        assert active_memory["replaces_id"]
        assert client.delete(f"/api/role/stories/{sid}/events/{speech['id']}").status_code == 200
        after = client.get(base + f"/runs/{sid}/record").json()
        assert after["initial"] is None and after["initial_omitted_for_redaction"]
        assert "钥匙在暗格" not in str(after)
        assert "快照中的匿名听闻" not in str(after)
        assert after["snapshots"] == []
        with db.session() as s:
            assert s.scalar(select(func.count()).select_from(Task)) == initial_task_count + 2
            assert s.get(Story, sid).seq == original_seq
        assert client.get(base + "/runs/missing/record").status_code == 404
        with db.session.begin() as s:
            s.get(Story, sid).owner_id = "foreign"
        assert client.get(base + f"/runs/{sid}/record").status_code == 404


def test_run_list_rename_and_cross_story_costs(db, config, fake, runtime):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        first, second = setup(client, "one"), setup(client, "two")
        base = "/api/role/experiments"
        runs = client.get(base + "/runs").json()["runs"]
        assert len(runs) == 2 and all(r["has_initial"] for r in runs)
        sid = first["story_id"]
        title = next(r["title"] for r in runs if r["id"] == sid)
        response = client.patch(
            base + f"/runs/{sid}/title", json={"title": "片段观察实验A", "expected_title": title}
        )
        assert response.status_code == 200
        assert (
            client.patch(
                base + f"/runs/{sid}/title", json={"title": "过时修改", "expected_title": title}
            ).status_code
            == 409
        )
        with db.session.begin() as s:
            parent = runtime.enqueue(s, "npc_step", config.workspace_owner, {"story_id": sid})
            foreign = runtime.enqueue(
                s, "respond", config.workspace_owner, {}, session_id=second["player_id"], parent_id=parent.id
            )
            s.add(
                Usage(
                    owner_id=config.workspace_owner, task_id=foreign.id, role="dialogue", model="test", cost=1
                )
            )
            original_revision = s.get(Conversation, first["player_id"]).revision
        record = client.get(base + f"/runs/{sid}/record").json()
        assert record["story"]["title"] == "片段观察实验A"
        assert len(record["tasks"]) == 1 and record["cost"]["settled_usd"] == 0
        with db.session() as s:
            assert s.get(Conversation, first["player_id"]).revision == original_revision
        assert client.get(base + "/runs?before=missing").status_code == 404
        config.product = "office"
        assert client.get(base + "/runs").status_code == 404
