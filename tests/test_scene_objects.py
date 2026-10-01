from types import SimpleNamespace

import pytest

from chatbot.experiment_templates import template
from chatbot.narrative import SceneObject
from chatbot.presentation import scene_objects
from chatbot.story_blueprints import Recipe
from chatbot.story_packages import references


def decoration(**changes):
    return {"id": "lamp", "kind": "decoration", "name": "灯", "sprite": "a" * 64,
            "transform": {"x": 400, "y": 800}, **changes}


def test_scene_objects_references_versions_and_assets():
    recipe = template("ferry-narrative")
    recipe["narrative"]["nodes"][0]["objects"] = [decoration()]
    parsed = Recipe.model_validate(recipe)
    assert "a" * 64 in references(parsed)
    assert parsed.narrative.nodes[0].objects[0].version == 1
    for obj in [decoration(version=2), decoration(kind="item"), decoration(item="bad")]:
        with pytest.raises(ValueError):
            SceneObject.model_validate(obj)
    recipe["narrative"]["nodes"][0]["objects"] = [decoration(), decoration()]
    with pytest.raises(ValueError):
        Recipe.model_validate(recipe)
    recipe["narrative"]["nodes"][0]["objects"] = [decoration(kind="item", item="missing")]
    with pytest.raises(ValueError):
        Recipe.model_validate(recipe)


def test_projection_uses_physical_visibility_and_runtime_ids(monkeypatch):
    from chatbot.items import execution, persistence
    from chatbot.items.models import World
    from chatbot.items.perception import visible_items

    world = World.model_validate({"actors": [{"id": "player", "location": "room"}], "items": [
        {"id": "box-live", "definition": {"id": "box", "name": "箱", "capabilities": {"can_contain": True}, "container": {}}, "placement": {"kind": "location", "target": "room"}},
        {"id": "key-live", "definition": {"id": "key", "name": "钥匙"}, "placement": {"kind": "container", "target": "box-live"}},
    ]})
    monkeypatch.setattr(persistence, "load", lambda *a, **kw: (None, world))
    monkeypatch.setattr(execution, "view", lambda s, a, w: visible_items(w, a.id, set()))
    session = SimpleNamespace(get=lambda *a: object())
    actor = SimpleNamespace(id="player")
    story = SimpleNamespace(id="story", settings={"item_aliases": {"key": "key-live"}})
    node = {"objects": [decoration(), decoration(id="key-picture", kind="item", item="key", editor_hidden=True, editor_locked=True)]}
    assert [o["id"] for o in scene_objects(session, actor, story, node)] == ["lamp"]
    world.items[0].state.open = True
    assert [o["id"] for o in scene_objects(session, actor, story, node)] == ["lamp"]
    world.items[1].placement.kind = "location"
    world.items[1].placement.target = "room"
    result = scene_objects(session, actor, story, node)
    assert result[1]["item_id"] == "key-live"
    assert not {"item", "editor_hidden", "editor_locked"} & result[1].keys()
    world.items[1].placement.kind = "actor"
    world.items[1].placement.target = "player"
    assert [o["id"] for o in scene_objects(session, actor, story, node)] == ["lamp"]
    world.items[1].placement.kind = "location"
    world.items[1].placement.target = "room"
    world.items[1].state.destroyed = True
    assert len(scene_objects(session, actor, story, node)) == 1
    assert scene_objects(session, actor, story, {**node, "mode": "backstage"}) == []
