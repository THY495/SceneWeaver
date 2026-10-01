from fastapi.testclient import TestClient
from sqlalchemy import func, select

from chatbot.api import create_app
from chatbot.storage import RoleMemory, SceneRecord


def test_preparation_retry_conflict_and_goal_filter(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        initial = client.post(
            "/api/role/experiments/start",
            json={"request_id": "prepare", "template_id": "partial-hearing"},
        ).json()
        sid, aid = initial["story_id"], initial["actors"]["restorer"]
        path = f"/api/role/actors/{aid}"
        roster = client.get(f"/api/role/stories/{sid}/roster").json()
        revision = next(p["revision"] for p in roster if p["id"] == aid)
        location = {"location": "门厅", "expected_revision": revision, "request_id": "move"}
        assert client.patch(path + "/location", json=location).status_code == 200
        assert client.patch(path + "/location", json=location).status_code == 200
        assert client.patch(path + "/location", json={**location, "location": "天台"}).status_code == 409
        assert client.patch(path + "/location", json={**location, "request_id": "stale"}).status_code == 409
        goal = {"kind": "goal", "content": "核实楼上的盒子", "request_id": "goal"}
        first = client.post(path + "/memories", json=goal)
        assert first.status_code == 201, first.text
        assert client.post(path + "/memories", json=goal).json() == first.json()
        assert client.post(path + "/memories", json={**goal, "content": "另一目标"}).status_code == 409
        with db.session.begin() as s:
            assert (
                s.scalar(
                    select(func.count())
                    .select_from(SceneRecord)
                    .where(
                        SceneRecord.story_id == sid, SceneRecord.dedupe == f"location-authoring:{aid}:move"
                    )
                )
                == 1
            )
            memory = s.get(RoleMemory, first.json()["id"])
            for index in range(105):
                s.add(
                    RoleMemory(
                        story_id=sid,
                        actor_id=aid,
                        kind="episode",
                        content=f"经历{index}",
                        source_ids=memory.source_ids,
                    )
                )
        filtered = client.get(path + "/memories?kind=goal").json()
        assert first.json()["id"] in {m["id"] for m in filtered["memories"]}
        assert all(m["kind"] == "goal" for m in filtered["memories"])
        assert filtered["next_before"] is None
        assert client.get(path + "/memories?kind=invalid").status_code == 400
