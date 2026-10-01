"""An explicit storage-only validation boundary for unfinished authoring graphs.

Runtime, validation and distribution endpoints continue using strict Recipe.
Draft context only relaxes reachability, blank openings and unfinished AI conditions, never
references, identifiers, cycles, inventory or character knowledge boundaries.
"""

from pydantic import Field, field_validator

from .story_blueprints import Recipe, StrictInput


def draft_recipe(value):
    return Recipe.model_validate(value, context={"authoring_draft": True})


class DraftBlueprintSave(StrictInput):
    expected_version: int = Field(ge=0)
    recipe: Recipe

    @field_validator("recipe", mode="before")
    @classmethod
    def read_draft(cls, value):
        return draft_recipe(value)
