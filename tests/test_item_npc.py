from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention
from test_item_runtime import recipe

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.storage import ItemWorldState, SceneRecord


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("wrong_protocol", [False, True])
async def test_npc_item_action_commits_or_rejects_without_mutation(
    db, config, runtime, fake, locked, wrong_protocol
):
    config.product = "roleplay"
    value = recipe()
    for p in [*value["people"], value["player"]]:
        p["location"] = "main"
    for a in value["item_world"]["actors"]:
        a["location"] = "main"
    value["item_world"]["items"][0]["placement"]["target"] = "main"
    value["item_world"]["items"][0]["state"]["locked"] = locked
    value["speech"] = []
    with TestClient(create_app(config, db, fake)) as client:
        bid = str(uuid4())
        saved = client.put(
            f"/api/role/experiments/blueprints/{bid}", json={"expected_version": 0, "recipe": value}
        )
        assert saved.status_code == 200, saved.text
        run = client.post(
            "/api/role/experiments/start",
            json={"request_id": "npc-items", "blueprint_id": bid, "blueprint_version": 1},
        ).json()
        sid, player = run["story_id"], run["player_id"]
        path = f"/api/role/stories/{sid}/settings"
        settings = client.get(path).json()
        client.put(path, json={**settings, "paused": False})
        bag = client.get(f"/api/role/items/actors/{player}").json()["items"][0]
        fake.replies["role_intent"] = intention(
            player,
            act="item_action",
            item_action={"item_id": bag["id"], "expected_revision": 1, "action": "open"},
        )
        if wrong_protocol:
            plans = iter(
                [
                    intention(
                        player,
                        act="object_action",
                        object_action={"object_id": bag["id"], "expected_version": 1, "verb": "take"},
                    ),
                    fake.replies["role_intent"],
                ]
            )
            fake.replies["role_intent"] = lambda _: next(plans)
        runtime.submit(player, config.workspace_owner, "馆长，请打开背包。", "open-bag")
        task = runtime.claim("foreground")
        agents = Agents(db, config, runtime, fake)
        result = await agents.respond(task)
        if not locked:
            assert "打开了日常背包" in result["answer"], result
        with db.session() as s:
            state = s.get(ItemWorldState, sid)
            assert state.revision == (1 if locked else 2)
            assert state.state["items"][0]["state"]["open"] is not locked
            events = list(s.scalars(select(SceneRecord).where(SceneRecord.task_id == task.id)))
            action = next(e for e in events if e.payload.get("item_action_result"))
            assert action.kind == "action"
            assert action.payload["item_action_result"]["status"] == ("rejected" if locked else "completed")
            if locked:
                assert action.payload["item_action_result"]["code"] == "locked"
        await agents.respond(task)
        with db.session() as s:
            assert s.get(ItemWorldState, sid).revision == (1 if locked else 2)
        assert not any(role == "dialogue" for role, _, _ in fake.calls)
        if wrong_protocol:
            assert len([role for role, _, _ in fake.calls if role == "role_intent"]) == 2
