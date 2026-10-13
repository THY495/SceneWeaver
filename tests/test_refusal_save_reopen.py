"""Controlled refusal through the normal Game save scope, without real inference."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention

from chatbot.game_library import create_game_library
from chatbot.game_seed import load_game_seed
from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Limit, Placement, World
from chatbot.narrative_demo import ferry_story
from chatbot.storage import SceneRecord
from chatbot.worker import Worker


async def test_refused_item_stays_player_owned_when_game_library_reopens(db, config, runtime, fake, tmp_path):
    config.product = "roleplay"
    recipe = ferry_story()
    recipe.pop("narrative")
    recipe["people"] = recipe["people"][:1]
    recipe["item_world"] = World(
        items=[instantiate("paper-sheet", "letter", Placement(kind="actor", target="player"))],
        actors=[
            ActorState(
                id=person["alias"],
                location="候船亭",
                lift_limit_g=Limit(mode="unlimited"),
                carry_limit_g=Limit(mode="unlimited"),
            )
            for person in [recipe["player"], *recipe["people"]]
        ],
    ).model_dump()
    package = tmp_path / "refusal.story.json"
    package.write_text(
        json.dumps(dict(format="sceneweaver.story.v3", recipe=recipe, assets={}, documents={})),
        encoding="utf-8",
    )
    source = await load_game_seed(db, config, package)
    web = tmp_path / "web"
    web.mkdir()
    (web / "assets").mkdir()
    (web / "game.html").write_text("Game", encoding="utf-8")

    def build():
        return create_game_library(config, db, fake, source_story_id=source, game_web_dir=web)

    with TestClient(build()) as client:
        assert client.post("/api/runtime/check").status_code == 200
        created = client.post("/api/game/saves", json={"request_id": "refusal", "label": "拒绝交接"})
        assert created.status_code == 201, created.text
        player = created.json()["id"]
        prefix = f"/runs/{player}"
        path = prefix + f"/api/role/items/actors/{player}"
        snapshot_path = prefix + f"/api/role/players/{player}/snapshot"
        assert client.put(prefix + "/api/game/pause", json={"paused": False}).status_code == 200
        before = client.get(path).json()
        paper = before["items"][0]
        target = before["recipients"][0]["id"]
        offered = client.post(
            path + "/offers",
            json={
                "request_id": str(uuid4()),
                "item_id": paper["id"],
                "expected_revision": paper["version"],
                "recipient_id": target,
            },
        )
        assert offered.status_code == 200, offered.text
        transfer = client.get(path).json()["transfers"][0]
        assert transfer["state"] == "wait_reply"
        # Only the model decision is controlled. Runtime agreement journaling,
        # observed stage projection and physical ownership use production code.
        fake.replies["role_intent"] = intention(
            player,
            act="agreement_decision",
            agreement_decision={
                "proposal_id": transfer["proposal_id"],
                "expected_version": transfer["version"],
                "item_ids": [transfer["item_id"]],
                "decision": "refuse",
            },
        )
        task = runtime.claim("refusal-reopen-contract")
        assert task.id == offered.json()["task_id"]
        await Worker(db, config, fake).run_task(task)
        after = client.get(path).json()
        assert after["transfers"][0]["state"] == "refused"
        assert after["items"] == before["items"]
        assert after["items"][0]["holder_id"] == player
        snapshot = client.get(snapshot_path).json()
        # Player projection deliberately omits internal task IDs. Bind through
        # the canonical event instead of inventing an exposed debug field.
        with db.session() as session:
            records = list(
                session.scalars(
                    select(SceneRecord).where(
                        SceneRecord.task_id == task.id,
                        SceneRecord.actor_id == target,
                        SceneRecord.kind == "speech",
                    )
                )
            )
            assert len(records) == 1
            event_id = records[0].id
        refusal = next(e for e in snapshot["events"] if e["id"] == event_id)
        assert refusal["stage"]["mode"] == "narration"
        assert "拒绝接收" in refusal["stage"]["text"]
        catalogue = client.get("/api/game/library").json()
        assert client.get("/api/role/projects").status_code == 404
    with TestClient(build()) as reopened:
        assert reopened.get("/api/game/library").json() == catalogue
        assert reopened.get(path).json() == after
        restored = reopened.get(snapshot_path).json()
        for key in ("frame", "events", "characters", "inventory"):
            assert restored[key] == snapshot[key]
        assert reopened.get("/api/role/projects").status_code == 404
