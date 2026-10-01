"""Explicit package capability boundary for directed performances."""

DIRECTED = "sceneweaver.story.v4"
MEDIA = "sceneweaver.story.v5"


def has_media(recipe):
    return bool(
        recipe.video_assets
        or (recipe.narrative and any(node.visual_mode != "stage" for node in recipe.narrative.nodes))
    )


def has_direction(recipe):
    return bool(recipe.narrative and any(node.direction is not None for node in recipe.narrative.nodes))


def package_format(recipe, *, documents=False):
    if has_media(recipe):
        return MEDIA
    if has_direction(recipe):
        return DIRECTED
    return "sceneweaver.story.v3" if documents else "sceneweaver.story.v2"
