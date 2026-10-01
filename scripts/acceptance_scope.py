"""Bound acceptance tools to a project or its verified independent-game save."""

from chatbot.experiment_initials import compatible_recipe_digests, digest
from chatbot.storage import ExperimentInitial, GameSave


def belongs_to_release(s, player, release, owner):
    """Bind a paid probe to the verified package, its immutable seed and saved recipe."""
    if not player or player.owner_id != owner or not player.current_state.get("player"):
        return False
    origin = s.get(ExperimentInitial, player.story_id)
    save = s.get(GameSave, player.id)
    source_id = origin.receipt.get("source_story_id") if origin else None
    source = s.get(ExperimentInitial, source_id) if source_id else None
    return bool(
        save
        and source
        and origin
        and save.owner_id == origin.owner_id == source.owner_id == owner
        and save.story_id == player.story_id
        and source.receipt.get("release_seed") is True
        and source.request_hash == source.receipt.get("package_hash") == release["package_sha256"]
        and save.release_id == digest(source.recipe)
        and digest(origin.recipe) in compatible_recipe_digests(source.recipe)
    )


def belongs_to_project(s, player, project_id):
    if not player or not project_id or not player.current_state.get("player"):
        return False
    origin = s.get(ExperimentInitial, player.story_id)
    if not origin or origin.owner_id != player.owner_id:
        return False
    if origin.receipt.get("blueprint_id") == project_id:
        return True
    save = s.get(GameSave, player.id)
    source_id = origin.receipt.get("source_story_id")
    source = s.get(ExperimentInitial, source_id) if source_id else None
    return bool(
        save
        and source
        and save.owner_id == player.owner_id == source.owner_id
        and save.story_id == player.story_id
        and source.receipt.get("blueprint_id") == project_id
        and save.release_id == digest(source.recipe)
        and digest(origin.recipe) in compatible_recipe_digests(source.recipe)
    )
