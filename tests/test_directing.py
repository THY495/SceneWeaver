from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from chatbot.directing import Beat, DirectorPlan, PerformanceJob, ready_jobs, validate_plan
from chatbot.narrative import Node, player_view


def test_direction_roundtrip_and_player_cannot_be_assigned(tmp_path):
    from chatbot.experiment_templates import template
    from chatbot.project_directory import open_project, save_project
    from chatbot.story_blueprints import Recipe

    recipe = template("ferry-narrative")
    segment = beat().model_dump()
    segment["participants"] = [recipe["people"][0]["alias"]]
    segment["role_focus"] = {segment["participants"][0]: "关注自己的航行判断"}
    direction = {"version": 1, "objective": "澄清分歧", "constraints": [], "beats": [segment]}
    recipe["narrative"]["nodes"][0]["direction"] = direction
    save_project(
        tmp_path / "directed",
        dict(format="sceneweaver.story.v3", recipe=recipe, assets={}, documents={}),
        expected_revision=0,
    )
    manifest, reopened = open_project(tmp_path / "directed")
    assert manifest["format"] == "sceneweaver.project.v2"
    assert reopened["format"] == "sceneweaver.story.v4"
    assert reopened["recipe"]["narrative"]["nodes"][0]["direction"] == direction
    segment["participants"] = [recipe["player"]["alias"]]
    segment["role_focus"] = {segment["participants"][0]: "关注旅人自己的选择"}
    with pytest.raises(ValidationError, match="不能替玩家"):
        Recipe.model_validate(recipe)


def test_directed_package_exports_explicit_version_and_imports(db, config, fake):
    import json

    from fastapi.testclient import TestClient

    from chatbot.api import create_app
    from chatbot.experiment_templates import template

    config.product = "roleplay"
    recipe = template("ferry-narrative")
    segment = beat().model_dump()
    segment["participants"] = [recipe["people"][0]["alias"]]
    segment["role_focus"] = {segment["participants"][0]: "关注自己的航行判断"}
    recipe["narrative"]["nodes"][0]["direction"] = {"objective": "澄清", "beats": [segment]}
    with TestClient(create_app(config, db, fake)) as client:
        response = client.post("/api/role/packages/export", json=recipe)
        assert response.status_code == 200, response.text
        package = response.json()
        assert package["format"] == "sceneweaver.story.v4"
        assert package["documents"] == {}
        result = client.post(
            "/api/role/packages/import", files={"file": ("story.json", json.dumps(package).encode())}
        )
        assert result.status_code == 200, result.text
        assert (
            result.json()["recipe"]["narrative"]["nodes"][0]["direction"]["beats"][0]["id"] == segment["id"]
        )
        assert (
            result.json()["recipe"]["narrative"]["nodes"][0]["direction"]["beats"][0]["role_focus"]
            == segment["role_focus"]
        )


def test_authored_director_starts_with_frozen_runtime_scope(db, config, runtime, fake):
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from chatbot.api import create_app
    from chatbot.experiment_templates import template
    from chatbot.npc.context import TurnContext
    from chatbot.npc.director import snapshot
    from chatbot.storage import ExperimentInitial

    config.product = "roleplay"
    recipe = template("ferry-narrative")
    segment = beat().model_dump()
    segment["participants"] = [recipe["people"][0]["alias"]]
    segment["role_focus"] = {segment["participants"][0]: "关注自己的航行判断"}
    recipe["narrative"]["nodes"][0]["direction"] = {"objective": "澄清", "beats": [segment]}
    pid = str(uuid4())
    with TestClient(create_app(config, db, fake)) as client:
        saved = client.put(f"/api/role/projects/{pid}", json={"expected_version": 0, "recipe": recipe})
        assert saved.status_code == 200
        result = client.post(
            "/api/role/experiments/start",
            json={"request_id": "directed", "blueprint_id": pid, "blueprint_version": 1},
        )
        assert result.status_code == 201, result.text
        receipt = result.json()
        player = receipt["player_id"]
        actors = tuple(a for a in receipt["actors"].values() if a != player)
        runtime.submit(player, config.workspace_owner, "请说说你的想法。", "directed-turn")
        task = runtime.claim("test")
        with db.session() as s:
            origin = s.get(ExperimentInitial, receipt["story_id"])
            assert origin.recipe["narrative"]["nodes"][0]["direction"]["objective"] == "澄清"
            scoped = snapshot(s, task, TurnContext(receipt["story_id"], player, actors))
            assert scoped is not None
            assert scoped.beat_id == segment["id"]
            assert len(scoped.eligible) == 1
            assert scoped.direction.beats[0].role_focus == {
                receipt["actors"][segment["participants"][0]]: "关注自己的航行判断"
            }


def test_incomplete_direction_is_draft_only():
    from chatbot.directing import SceneDirection

    value = {
        "objective": "",
        "beats": [{"id": "b", "title": "", "objective": "", "participants": [], "completion": ""}],
    }
    draft = SceneDirection.model_validate(value, context={"authoring_draft": True})
    assert not draft.beats[0].participants
    with pytest.raises(ValidationError):
        SceneDirection.model_validate(draft.model_dump())
    value["beats"][0]["participants"] = ["a", "a"]
    with pytest.raises(ValidationError, match="重复"):
        SceneDirection.model_validate(value, context={"authoring_draft": True})
    value["beats"][0]["participants"] = ["a"]
    value["beats"][0]["role_focus"] = {"b": "未参与角色"}
    with pytest.raises(ValidationError, match="参与角色"):
        SceneDirection.model_validate(value, context={"authoring_draft": True})


def beat():
    return Beat(
        id="dispute",
        title="澄清",
        objective="澄清钥匙去向",
        participants=["a", "b", "c"],
        completion="玩家选择下一步",
    )


def plan():
    return DirectorPlan(
        beat_id="dispute",
        reason="先解释再询问",
        jobs=[
            {"id": "a1", "actor": "a", "objective": "解释"},
            {"id": "b1", "actor": "b", "objective": "提供独立线索"},
            {"id": "c1", "actor": "c", "objective": "询问矛盾", "depends_on": ["a1", "b1"]},
        ],
    )


def test_dependencies_parallel_ready_and_scope():
    p = plan()
    assert [j.id for j in ready_jobs(p, set(), set())] == ["a1", "b1"]
    assert ready_jobs(p, {"a1"}, {"a1", "b1"}) == []
    assert [j.id for j in ready_jobs(p, {"a1", "b1"}, {"a1", "b1"})] == ["c1"]
    validate_plan(p, beat(), {"a", "b", "c"}, {})
    with pytest.raises(ValueError, match="不可参与"):
        validate_plan(p, beat(), {"a", "b"}, {})
    p.jobs[0].addressees = ["hidden"]
    with pytest.raises(ValueError, match="可感知"):
        validate_plan(p, beat(), {"a", "b", "c"}, {})


def test_runtime_job_cannot_forward_director_handoff_as_actor_objective():
    # A real run leaked this objective into a character's spoken line.
    with pytest.raises(ValidationError, match="后台动作"):
        PerformanceJob(id="ack", actor="a", objective="确认记录后自然把场景交还旅人。")
    assert PerformanceJob(id="ack", actor="a", objective="确认旅人的选择后自然结束回应。")


def test_cycle_forward_dependency_duplicate_and_new_version_rejected():
    raw = plan().model_dump()
    raw["jobs"][0]["depends_on"] = ["c1"]
    with pytest.raises(ValidationError):
        DirectorPlan.model_validate(raw)
    raw = plan().model_dump()
    raw["jobs"][1]["id"] = "a1"
    with pytest.raises(ValidationError):
        DirectorPlan.model_validate(raw)
    raw = plan().model_dump()
    raw["version"] = 2
    with pytest.raises(ValidationError):
        DirectorPlan.model_validate(raw)


def test_old_scene_compatible_and_direction_not_player_visible():
    node = Node(id="scene", title="场景", text="开场", observers=["player"])
    assert node.direction is None
    node = Node.model_validate(
        {
            **node.model_dump(),
            "direction": {
                "objective": "作者秘密",
                "constraints": ["不得泄漏真凶"],
                "beats": [beat().model_dump()],
            },
        }
    )
    story = SimpleNamespace(
        settings={"narrative": {"node": "scene", "path": ["scene"], "event_id": "entry"}}, seq=1
    )
    view = player_view(story, {"narrative": {"nodes": [node.model_dump()], "edges": []}})
    assert "direction" not in view
    assert "作者秘密" not in str(view)
