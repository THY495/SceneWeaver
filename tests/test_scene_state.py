import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention, launch, persona

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.providers import ProviderError
from chatbot.scene_state import transition
from chatbot.storage import Conversation, SceneRecord


def test_player_objects_versions_ownership_privacy_and_redaction(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        client.patch(f"/api/role/actors/{actors[1]}/location", json={"location": "upstairs"})
        body = {"request_id": "key", "name": "铜钥匙", "location": "main"}
        item = client.post(f"/api/role/stories/{story}/objects", json=body).json()
        assert client.post(f"/api/role/stories/{story}/objects", json=body).json()["id"] == item["id"]
        assert (
            client.post(f"/api/role/stories/{story}/objects", json={**body, "name": "别的"}).status_code
            == 400
        )
        assert client.get(f"/api/role/stories/{story}/objects?actor_id={actors[1]}").json() == []
        take = {"request_id": "take", "object_id": item["id"], "expected_version": 1, "verb": "take"}
        result = client.post(f"/api/role/actors/{player}/object-actions", json=take)
        assert result.status_code == 200, result.text
        assert result.json()["holder_id"] == player and result.json()["version"] == 2
        assert client.post(f"/api/role/actors/{player}/object-actions", json=take).json()["version"] == 2
        assert (
            client.post(
                f"/api/role/actors/{player}/object-actions", json={**take, "request_id": "stale"}
            ).status_code
            == 400
        )
        with db.session() as s:
            npc = s.get(Conversation, actors[0])
            with pytest.raises(ValueError):
                transition(s, npc, {**take, "expected_version": 2})
        give = {**take, "request_id": "give", "expected_version": 2, "verb": "give", "target_id": actors[1]}
        assert client.post(f"/api/role/actors/{player}/object-actions", json=give).status_code == 400
        give["target_id"] = actors[0]
        assert client.post(f"/api/role/actors/{player}/object-actions", json=give).json()["version"] == 3
        with db.session() as s:
            with pytest.raises(ValueError, match="玩家"):
                transition(
                    s, s.get(Conversation, actors[0]), {**give, "target_id": player, "expected_version": 3}
                )
        latest = client.get(f"/api/role/stories/{story}/objects").json()[0]
        assert client.delete(f"/api/role/stories/{story}/events/{latest['event_id']}").status_code == 200
        assert client.get(f"/api/role/stories/{story}/objects").json() == []


async def test_npc_action_commits_state_with_text_once(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        item = client.post(
            f"/api/role/stories/{story}/objects", json={"request_id": "key", "name": "铜钥匙"}
        ).json()
        fake.replies["role_intent"] = intention(
            player,
            act="object_action",
            object_action={"verb": "take", "object_id": item["id"], "expected_version": 1},
        )
        runtime.submit(player, config.workspace_owner, "馆长，请拿起钥匙", "take-key")
        task = runtime.claim("foreground")
        agents = Agents(db, config, runtime, fake)
        result = await agents.respond(task)
        assert result["answer"] == "拿起了铜钥匙。"
        assert not any(role == "dialogue" for role, _, _ in fake.calls)
        assert client.get(f"/api/role/stories/{story}/exchanges?actor_id={player}").json() == []
        assert client.get(f"/api/role/stories/{story}/objects").json()[0]["holder_id"] == actors[0]
        await agents.respond(task)
        assert client.get(f"/api/role/stories/{story}/objects").json()[0]["version"] == 2
        with db.session() as s:
            assert (
                len(
                    list(
                        s.scalars(
                            select(SceneRecord).where(
                                SceneRecord.task_id == task.id, SceneRecord.kind == "object_state"
                            )
                        )
                    )
                )
                == 1
            )


@pytest.mark.parametrize("choice", ["silence", "refuse", "defer"])
async def test_named_npc_response_is_committed_and_resolves_question(db, config, runtime, fake, choice):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        fake.replies["role_intent"] = lambda messages: (
            intention(actors[1], expects_reply=True, wait_for_player=False)
            if persona(messages) == "馆长"
            else intention(player, act="wait" if choice == "silence" else "speak", response_kind=choice)
        )
        fake.replies["dialogue"] = lambda messages: (
            "你愿意说说吗？"
            if persona(messages) == "馆长"
            else ("我不愿回答。" if choice == "refuse" else "我想稍后再说。")
        )
        runtime.submit(player, config.workspace_owner, "馆长先问修复师，让她回应", "exchange")
        result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
        assert len(result["segments"]) == 2 and result["interaction_complete"]
        rows = client.get(f"/api/role/stories/{story}/exchanges?actor_id={player}").json()
        assert rows[0]["status"] == choice and rows[0]["response_event_id"]


async def test_generation_failure_keeps_question_open(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        fake.replies["role_intent"] = lambda messages: intention(
            actors[1] if persona(messages) == "馆长" else player,
            wait_for_player=persona(messages) != "馆长",
            expects_reply=persona(messages) == "馆长",
        )

        def dialogue(messages):
            if persona(messages) != "馆长":
                raise ProviderError("synthetic failure")
            return "你愿意说说吗？"

        fake.replies["dialogue"] = dialogue
        runtime.submit(player, config.workspace_owner, "馆长问修复师", "failed-exchange")
        with pytest.raises(ProviderError):
            await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
        rows = client.get(f"/api/role/stories/{story}/exchanges?actor_id={player}").json()
        assert rows[0]["status"] == "open" and not rows[0]["response_event_id"]


def test_observation_refresh_is_private_and_idempotent(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        client.patch(f"/api/role/actors/{actors[1]}/location", json={"location": "upstairs"})
        item = client.post(
            f"/api/role/stories/{story}/objects", json={"request_id": "key", "name": "铜钥匙"}
        ).json()
        client.post(
            f"/api/role/actors/{player}/object-actions",
            json={"request_id": "take", "object_id": item["id"], "expected_version": 1, "verb": "take"},
        )
        client.patch(f"/api/role/actors/{actors[1]}/location", json={"location": "main"})
        assert client.get(f"/api/role/stories/{story}/objects?actor_id={actors[1]}").json() == []
        url = f"/api/role/actors/{actors[1]}/observe"
        seen = client.post(url, json={"request_id": "look"})
        assert seen.status_code == 200, seen.text
        assert seen.json()[0]["holder_id"] == player and seen.json()[0]["version"] == 2
        assert client.post(url, json={"request_id": "look"}).json() == seen.json()
        from chatbot.observations import visible_records

        with db.session() as s:
            records = visible_records(s, actors[1])
            assert not any(r.dedupe == f"player-object:{player}:take" for r in records)


async def test_explicit_player_recipient_can_choose_visible_silence(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        fake.replies["role_intent"] = intention(player, act="wait")
        runtime.submit(
            player, config.workspace_owner, "馆长，你愿意解释吗？", "direct", addressees=[actors[0]]
        )
        result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
        assert result["interaction_complete"] and "沉默" in result["answer"]
        rows = client.get(f"/api/role/stories/{story}/exchanges?actor_id={player}").json()
        assert rows[0]["status"] == "silence"


def test_concurrent_take_has_only_one_winner(db, config, fake):
    from concurrent.futures import ThreadPoolExecutor

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        item = client.post(
            f"/api/role/stories/{story}/objects", json={"request_id": "key", "name": "铜钥匙"}
        ).json()

        def take(n):
            return client.post(
                f"/api/role/actors/{player}/object-actions",
                json={"request_id": str(n), "object_id": item["id"], "expected_version": 1, "verb": "take"},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(take, [1, 2])) == [200, 400]
        assert client.get(f"/api/role/stories/{story}/objects").json()[0]["version"] == 2
