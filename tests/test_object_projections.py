from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch

from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.scene_state import objects
from chatbot.storage import Conversation, SceneRecord


def test_observing_does_not_replace_authoritative_state(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
        item = client.post(
            f"/api/role/stories/{sid}/objects",
            json={"request_id": "create", "name": "盒子", "location": "main"},
        ).json()
        before = client.get(f"/api/role/stories/{sid}/objects").json()
        seen = client.post(f"/api/role/actors/{player}/observe", json={"request_id": "look"}).json()
        assert seen[0]["event_id"] != before[0]["event_id"]
        assert client.get(f"/api/role/stories/{sid}/objects").json() == before
        # Removing one's sighting cannot delete the actual object.
        assert client.delete(f"/api/role/stories/{sid}/events/{seen[0]['event_id']}").status_code == 200
        assert client.get(f"/api/role/stories/{sid}/objects").json() == before
        result = client.post(
            f"/api/role/actors/{player}/object-actions",
            json={"request_id": "take", "object_id": item["id"], "expected_version": 1, "verb": "take"},
        )
        # The observation redaction invalidates this actor's view, not the world.
        assert result.status_code == 400
        client.post(f"/api/role/actors/{player}/observe", json={"request_id": "look-again"})
        result = client.post(
            f"/api/role/actors/{player}/object-actions",
            json={"request_id": "take", "object_id": item["id"], "expected_version": 1, "verb": "take"},
        )
        assert result.status_code == 200
        assert result.json()["version"] == 2


def test_legacy_sighting_cannot_overwrite_world_or_leak_new_state(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
        item = client.post(
            f"/api/role/stories/{sid}/objects",
            json={"request_id": "create", "name": "钥匙", "location": "main"},
        ).json()
        original = client.get(f"/api/role/stories/{sid}/objects").json()[0]
        # Exercise archives written before object_record_role existed.
        with db.session.begin() as s:
            append_record(
                s,
                story_id=sid,
                kind="object_state",
                text="旧观察",
                observer_ids=[player],
                actor=s.get(Conversation, player),
                channel="private",
                dedupe=f"observe-object:{player}:old:{item['id']}",
                payload={"object": {**item, "location": "old-place"}},
            )
            append_record(
                s,
                story_id=sid,
                kind="object_state",
                text="旧搜索",
                observer_ids=[actors[0]],
                actor=s.get(Conversation, actors[0]),
                channel="private",
                dedupe="old-search",
                payload={"object": {**item, "location": "searched-place"}, "intent": {"act": "search"}},
            )
        assert client.get(f"/api/role/stories/{sid}/objects").json() == [original]
        assert (
            client.get(f"/api/role/stories/{sid}/objects?actor_id={player}").json()[0]["location"]
            == "old-place"
        )
        assert (
            client.get(f"/api/role/stories/{sid}/objects?actor_id={actors[0]}").json()[0]["location"]
            == "searched-place"
        )
        assert (
            client.get(f"/api/role/stories/{sid}/objects?actor_id={actors[1]}").json()[0]["location"]
            == "main"
        )


def test_observation_preserves_unexposed_authoritative_metadata(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
        client.post(f"/api/role/stories/{sid}/objects", json={"request_id": "create", "name": "箱"})
        with db.session.begin() as s:
            record = s.scalar(
                select(SceneRecord).where(
                    SceneRecord.story_id == sid, SceneRecord.dedupe == "object-create:create"
                )
            )
            record.payload = {
                **record.payload,
                "object": {
                    **record.payload["object"],
                    "future_container_state": {"contents": ["private-letter"]},
                },
            }
        seen = client.post(f"/api/role/actors/{player}/observe", json={"request_id": "look"})
        assert "future_container_state" not in seen.json()[0]
        with db.session() as s:
            assert objects(s, sid)[0]["future_container_state"] == {"contents": ["private-letter"]}
