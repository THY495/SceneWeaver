import math

import pytest

from chatbot.experiment_templates import template
from chatbot.story_blueprints import Recipe


def test_editor_position_is_optional_bounded_and_preserved():
    value = template("ferry-narrative")
    assert Recipe.model_validate(value).narrative.nodes[0].editor_position is None
    value["narrative"]["nodes"][0]["editor_position"] = {"x": -120.5, "y": 420}
    result = Recipe.model_validate(value).model_dump()
    assert result["narrative"]["nodes"][0]["editor_position"] == {"x": -120.5, "y": 420}
    for invalid in (math.inf, math.nan, 100001):
        value["narrative"]["nodes"][0]["editor_position"]["x"] = invalid
        with pytest.raises(ValueError):
            Recipe.model_validate(value)


def test_stage_transform_version_bounds_and_legacy():
    from chatbot.narrative import StagePlacement

    assert StagePlacement(actor="npc").transform is None
    transform = {"version": 1, "x": 420, "y": 760, "rotation": -12, "scale_x": 0.7}
    placement = StagePlacement(actor="npc", transform=transform)
    assert placement.transform.scale_y == 1
    assert StagePlacement.model_validate(placement.model_dump()) == placement
    for field, value in [("version", 2), ("x", math.nan), ("y", math.inf), ("scale_x", 0), ("rotation", 181)]:
        with pytest.raises(ValueError):
            StagePlacement(actor="npc", transform={**transform, field: value})
