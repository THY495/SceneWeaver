from fastapi.testclient import TestClient
from support.story import launch

from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.role_index import schedule_index
from chatbot.storage import Conversation, Task
from chatbot.worker import Worker


def add_visible_events(db, story_id, actor_id, count=5):
    with db.session.begin() as s:
        actor = s.get(Conversation, actor_id)
        for index in range(count):
            append_record(
                s,
                story_id=story_id,
                kind="speech",
                text=f"第{index + 1}条可见发言",
                actor=actor,
                observer_ids=[actor_id],
                dedupe=f"snapshot-event-{story_id}-{index}",
            )


async def test_creator_can_force_and_observe_role_snapshot(db, config, runtime, fake):
    config.product = "roleplay"
    config.embedding_provider = "openai"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = launch(client)
        assert client.put(f"/api/role/stories/{story}/settings", json={"paused": True}).status_code == 200
        add_visible_events(db, story, actors[0])
        path = f"/api/role/stories/{story}/memory-snapshots"
        body = {"actor_id": actors[0], "request_id": "first-pass"}
        created = client.post(path, json=body)
        assert created.status_code == 200, created.text
        assert created.json()["scheduled"]
        tid = created.json()["task_id"]
        assert client.post(path, json=body).json()["task_id"] == tid
        assert client.post(path, json={**body, "actor_id": actors[1]}).status_code == 409

        task = runtime.claim("snapshot-test", background=True)
        assert task.id == tid and task.kind == "role_reflect"
        await Worker(db, config, fake).run_task(task)

        state = client.get(path + f"/{tid}")
        assert state.status_code == 200, state.text
        result = state.json()
        assert result["status"] == "completed"
        assert result["cut"] == 3
        assert result["snapshot_id"]
        frame = client.get(
            f"/api/role/stories/{story}/observation-frame", params={"actor_id": actors[0]}
        ).json()
        assert frame["snapshot"]["id"] == result["snapshot_id"]
        assert frame["snapshot"]["covered_seq"] == 3
        assert len(frame["snapshot"]["source_ids"]) == 3
        with db.session() as s:
            assert s.get(Task, tid).result["embedding_deferred"] is True
        index_task = schedule_index(db, config, runtime)
        assert index_task and index_task.kind == "role_index"


def test_snapshot_experiment_requires_paused_npc_and_enough_events(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        path = f"/api/role/stories/{story}/memory-snapshots"
        assert (
            client.post(path, json={"actor_id": actors[0], "request_id": "running"}).status_code == 409
        )
        assert client.put(f"/api/role/stories/{story}/settings", json={"paused": True}).status_code == 200
        player_result = client.post(path, json={"actor_id": player, "request_id": "player"})
        assert player_result.status_code == 404
        empty = client.post(path, json={"actor_id": actors[0], "request_id": "empty"})
        assert empty.status_code == 200
        assert empty.json()["scheduled"] is False

        other, other_actors, _ = launch(client)
        assert client.put(f"/api/role/stories/{other}/settings", json={"paused": True}).status_code == 200
        assert (
            client.post(path, json={"actor_id": other_actors[0], "request_id": "foreign"}).status_code
            == 404
        )
