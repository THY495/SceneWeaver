from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from chatbot.narrative import Node, player_view


def test_cg_requires_image_for_runtime_but_allows_incomplete_draft():
    node = {"id": "room", "title": "房间", "text": "夜色", "observers": ["player", "npc"]}
    assert Node.model_validate(node).visual_mode == "stage"
    node["visual_mode"] = "cg"
    assert Node.model_validate(node, context={"authoring_draft": True}).background is None
    with pytest.raises(ValidationError, match="CG 场景"):
        Node.model_validate(node)
    node.update(background="a" * 64, stage=[{"actor": "npc"}])
    parsed = Node.model_validate(node)
    assert parsed.observers == ["player", "npc"]
    assert parsed.stage[0].actor == "npc"
    assert Node.model_validate(parsed.model_dump()).visual_mode == "cg"


def test_backstage_cg_never_exposes_image_or_mode_to_player():
    story = SimpleNamespace(settings={"narrative": {"node": "secret", "path": ["secret"]}})
    recipe = {
        "narrative": {
            "nodes": [{"id": "secret", "mode": "backstage", "visual_mode": "cg", "background": "a" * 64}]
        }
    }
    result = player_view(story, recipe)
    assert result["mode"] == "backstage"
    assert "background" not in result
    assert "visual_mode" not in result
