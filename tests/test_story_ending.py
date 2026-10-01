from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.narrative import Narrative, Node
from chatbot.scene_tasks import active
from chatbot.storage import Story


def ending():
    return dict(
        id="end",
        title="归航",
        observers=["player"],
        ending=True,
        opening=[dict(id="last", kind="narration", text="渡船驶离码头。故事结束。")],
    )


def test_ending_is_explicit_and_cannot_hide_more_story():
    assert not Node.model_validate({**ending(), "ending": False}).ending
    with pytest.raises(ValidationError, match="结束节点"):
        Node.model_validate({**ending(), "mode": "backstage"})
    with pytest.raises(ValidationError, match="结束节点不能"):
        Narrative.model_validate(
            {
                "entry": "end",
                "nodes": [ending(), {**ending(), "id": "next"}],
                "edges": [dict(id="e", source="end", target="next", label="继续")],
            }
        )


def test_authored_finale_finishes_idempotently_and_blocks_new_work(db, config, fake, runtime):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        recipe = template("partial-hearing")
        recipe["narrative"] = {"entry": "end", "nodes": [ending()], "edges": []}
        bid = str(uuid4())
        base = "/api/role/experiments"
        saved = client.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe})
        assert saved.status_code == 200, saved.text
        result = client.post(
            base + "/start", json={"request_id": "ending", "blueprint_id": bid, "blueprint_version": 1}
        )
        assert result.status_code == 201, result.text
        run = result.json()
        sid, player = run["story_id"], run["player_id"]
        path = base + f"/stories/{sid}/narrative"
        before = client.get(path).json()
        assert before["ending"] and not before["finished_at"]
        settings_url = f"/api/role/stories/{sid}/settings"
        settings = client.get(settings_url).json()
        client.put(settings_url, json={**settings, "paused": False})
        body = dict(request_id="finish", node_id="end", expected_version=1)
        response = client.post(path + "/advance", json=body)
        assert response.status_code == 200, response.text
        finished = response.json()
        assert finished["finished_at"] and finished["execution"]["phase"] == "finished"
        listed = client.get(base + "/runs", params={"blueprint_id": bid}).json()["runs"]
        assert listed[0]["id"] == sid and listed[0]["finished_at"] == finished["finished_at"]
        record = client.get(base + f"/runs/{sid}/record").json()
        assert record["story"]["finished_at"] == finished["finished_at"]
        assert client.post(path + "/advance", json=body).json() == finished
        snapshot = client.get(f"/api/role/players/{player}/snapshot").json()
        assert not snapshot["interaction"]["allowed"]
        assert snapshot["interaction"]["reason"] == "故事已结束。"
        assert (
            client.post(f"/api/role/stories/{sid}/presence", json={"visible": True, "typing": False}).json()[
                "reason"
            ]
            == "finished"
        )
        with pytest.raises(HTTPException, match="故事已结束"):
            runtime.submit(player, config.workspace_owner, "继续", "after-ending")
        with db.session() as s:
            assert not active(s.get(Story, sid))
        assert (
            client.post(
                f"/api/role/stories/{sid}/steps",
                json={"request_id": "after-end", "actor_id": run["actors"]["curator"]},
            ).status_code
            == 409
        )
