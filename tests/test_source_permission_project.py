"""Creator grants survive project revisions without deleting author constraints."""

from copy import deepcopy

from chatbot.directing import SceneDirection
from chatbot.experiment_templates import template
from chatbot.project_directory import open_project, save_project
from chatbot.source_authorization import permits_source_tasks


def test_source_permission_save_reopen_revoke_preserves_author_requirements(tmp_path):
    recipe = template("ferry-narrative")
    actor = next(p["alias"] for p in recipe["people"] if p["alias"] != recipe["player"])
    direction = {
        "version": 1, "objective": "回应实际问题", "constraints": ["保留作者的边界"],
        "beats": [{"id": "reply", "title": "交谈", "objective": "回答来访者", "completion": "得到回应",
                   "participants": [actor], "role_focus": {actor: "考虑自身意愿"}, "constraints": ["不得代替玩家决定"],
                   "source_response_tasks": True, "handoff": "after_beat", "max_segments": 1}],
    }
    recipe["narrative"]["nodes"][0]["direction"] = direction
    package = {"format": "sceneweaver.story.v2", "recipe": recipe, "assets": {}}
    root = tmp_path / "creator-project"
    first = save_project(root, package, expected_revision=0)
    _, reopened = open_project(root)
    stored = reopened["recipe"]["narrative"]["nodes"][0]["direction"]
    assert stored["beats"][0]["source_response_tasks"] is True
    assert stored["constraints"] == direction["constraints"]
    assert stored["beats"][0]["constraints"] == direction["beats"][0]["constraints"]
    assert stored["beats"][0]["role_focus"] == direction["beats"][0]["role_focus"]
    parsed = SceneDirection.model_validate(stored)
    assert not permits_source_tasks(parsed, parsed.beats[0])
    prior = (root / "content" / (first["content"] + ".json")).read_bytes()
    revoked = deepcopy(reopened)
    revoked["recipe"]["narrative"]["nodes"][0]["direction"]["beats"][0]["source_response_tasks"] = False
    save_project(root, revoked, expected_revision=1)
    manifest, second = open_project(root)
    assert manifest["revision"] == 2
    assert second["recipe"]["narrative"]["nodes"][0]["direction"]["beats"][0]["source_response_tasks"] is False
    assert second["recipe"]["narrative"]["nodes"][0]["direction"]["constraints"] == direction["constraints"]
    assert (root / "content" / (first["content"] + ".json")).read_bytes() == prior
