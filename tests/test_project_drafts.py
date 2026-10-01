from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.project_directory import open_project, save_project
from chatbot.project_drafts import draft_recipe
from chatbot.storage import Story, StoryBlueprint
from chatbot.story_blueprints import Recipe


def unfinished():
    recipe = template("ferry-narrative")
    graph = recipe["narrative"]
    graph["edges"] = [
        dict(
            id="unfinished",
            source=graph["entry"],
            target=graph["nodes"][1]["id"],
            label="待编写",
            trigger="ai",
            condition="",
        )
    ]
    return recipe


def test_draft_round_trip_preserves_unfinished_graph(tmp_path):
    original = unfinished()
    package = dict(format="sceneweaver.story.v3", recipe=original, assets={}, documents={})
    save_project(tmp_path / "draft", package, expected_revision=0)
    _, reopened = open_project(tmp_path / "draft")
    assert len(reopened["recipe"]["narrative"]["nodes"]) == len(original["narrative"]["nodes"])
    assert reopened["recipe"]["narrative"]["edges"][0]["condition"] == ""
    with pytest.raises(ValidationError, match="AI 转场"):
        Recipe.model_validate(reopened["recipe"])
    reopened["recipe"]["narrative"]["edges"][0]["condition"] = "玩家离开"
    with pytest.raises(ValidationError, match="入口到达"):
        Recipe.model_validate(reopened["recipe"])


def test_empty_scene_opening_is_draft_only():
    recipe = template("ferry-narrative")
    recipe["narrative"]["nodes"][0]["opening"] = None
    recipe["narrative"]["nodes"][0]["text"] = ""
    assert draft_recipe(recipe).narrative.nodes[0].text == ""
    with pytest.raises(ValidationError, match="开场积木"):
        Recipe.model_validate(recipe)


@pytest.mark.parametrize("damage", ["cycle", "missing", "duplicate", "actor"])
def test_drafts_do_not_relax_structural_safety(damage):
    recipe = unfinished()
    graph = recipe["narrative"]
    edge = graph["edges"][0]
    if damage == "cycle":
        graph["edges"].append({**edge, "id": "back", "source": edge["target"], "target": edge["source"]})
    elif damage == "missing":
        edge["target"] = "missing-node"
    elif damage == "duplicate":
        graph["nodes"].append(deepcopy(graph["nodes"][0]))
    else:
        graph["nodes"][0]["stage"] = [dict(actor="missing-actor")]
    with pytest.raises(ValidationError):
        draft_recipe(recipe)


def test_disk_draft_can_reopen_but_not_run_or_distribute(db, config, fake):
    config.product = "roleplay"
    pid = str(uuid4())
    with TestClient(create_app(config, db, fake)) as client:
        url = "/api/role/projects/" + pid
        saved = client.put(url, json=dict(expected_version=0, recipe=unfinished()))
        assert saved.status_code == 200, saved.text
        with db.session.begin() as s:
            s.delete(s.get(StoryBlueprint, pid))
        reopened = client.get(url)
        assert reopened.status_code == 200, reopened.text
        recipe = reopened.json()["recipe"]
        assert client.get("/api/role/projects").json()[0]["recipe"] == recipe
        assert (
            client.post(
                "/api/role/experiments/validate", json=dict(expected_version=1, recipe=recipe)
            ).status_code
            == 422
        )
        assert client.post("/api/role/packages/export", json=recipe).status_code == 422
        run = client.post(
            "/api/role/experiments/start",
            json=dict(request_id="draft-preview", blueprint_id=pid, blueprint_version=1),
        )
        assert run.status_code == 422, run.text
        assert "工程尚不能运行" in run.text
        with db.session() as s:
            assert s.scalar(select(func.count()).select_from(Story)) == 0
        fixed = client.put(url, json=dict(expected_version=1, recipe=template("ferry-narrative")))
        assert fixed.status_code == 200, fixed.text
        run = client.post(
            "/api/role/experiments/start",
            json=dict(request_id="draft-preview", blueprint_id=pid, blueprint_version=2),
        )
        assert run.status_code == 201, run.text
