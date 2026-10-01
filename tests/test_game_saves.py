import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from chatbot.api import create_app
from chatbot.game_saves import GameSaves, NewGame
from chatbot.storage import ExperimentInitial, GameSave, Story


def test_game_save_migration_creates_catalogue_and_can_be_reapplied(db):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    path = Path(__file__).resolve().parents[1] / "migrations/versions/018_game_saves.py"
    spec = importlib.util.spec_from_file_location("migration018", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    # Only the dedicated db fixture is used; it already enforces an _test database.
    GameSave.__table__.drop(db.engine)
    with db.engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.upgrade()
        migration.upgrade()
        schema = inspect(conn)
        assert {c["name"] for c in schema.get_columns("game_saves")} == {
            "player_id",
            "story_id",
            "owner_id",
            "release_id",
            "request_id",
            "label",
            "created_at",
        }
        assert {tuple(c["column_names"]) for c in schema.get_unique_constraints("game_saves")} == {
            ("story_id",),
            ("owner_id", "release_id", "request_id"),
        }


def test_game_new_save_is_atomic_scoped_and_recoverable(db, config, fake):
    config.product = "roleplay"
    app = create_app(config, db, fake, serve_web=False)
    with TestClient(app) as client:
        source = client.post(
            "/api/role/experiments/start",
            json={
                "request_id": "source",
                "template_id": "partial-hearing",
            },
        ).json()

    async def ready():
        pass

    app.state.runtime.model_readiness.require = ready
    library = GameSaves(db, config, app.state.runtime, source_story_id=source["story_id"])
    assert library.list() == []  # Creator previews are not silently promoted to player saves.
    with db.session.begin() as s:
        source_story = s.get(Story, source["story_id"])
        source_story.settings = {**source_story.settings, "paused": False, "source_only_progress": True}
    first = asyncio.run(library.create(NewGame(request_id="first", label="第一次旅程")))
    import importlib.util
    from pathlib import Path

    from chatbot.storage import Conversation

    scope_spec = importlib.util.spec_from_file_location(
        "acceptance_scope", Path(__file__).resolve().parents[1] / "scripts/acceptance_scope.py"
    )
    scope_module = importlib.util.module_from_spec(scope_spec)
    scope_spec.loader.exec_module(scope_module)
    with db.session.begin() as s:
        origin = s.get(ExperimentInitial, source["story_id"])
        origin.receipt = {**origin.receipt, "blueprint_id": "project-test"}
    with db.session() as s:
        player = s.get(Conversation, first["id"])
        assert scope_module.belongs_to_project(s, player, "project-test")
        assert not scope_module.belongs_to_project(s, player, "different-project")
        assert not scope_module.belongs_to_project(
            s, s.get(Conversation, source["actors"]["reporter"]), "project-test"
        )
    assert set(first) == {"id", "label", "created_at", "finished"}
    assert asyncio.run(library.create(NewGame(request_id="first", label="第一次旅程"))) == first
    with pytest.raises(HTTPException) as conflict:
        asyncio.run(library.create(NewGame(request_id="first", label="changed")))
    assert conflict.value.status_code == 409
    second = asyncio.run(library.create(NewGame(request_id="second")))
    assert second["id"] != first["id"]
    restarted = GameSaves(db, config, app.state.runtime, source_story_id=source["story_id"])
    assert {v["id"] for v in restarted.list()} == {first["id"], second["id"]}
    with db.session() as s:
        row, story = library.owned(s, first["id"])
        assert not story.settings["paused"] and "source_only_progress" not in story.settings
        assert s.scalar(select(func.count()).select_from(GameSave)) == 2
        with pytest.raises(HTTPException):
            library.owned(s, source["player_id"])
        with pytest.raises(HTTPException):
            library.owned(s, source["actors"]["reporter"])
    with db.session.begin() as s:
        origin = s.get(ExperimentInitial, source["story_id"])
        origin.recipe = {**origin.recipe, "title": "A different release"}
    with pytest.raises(HTTPException) as changed:
        asyncio.run(library.create(NewGame(request_id="third")))
    assert changed.value.status_code == 409
    other_release = GameSaves(db, config, app.state.runtime, source_story_id=source["story_id"])
    assert other_release.list() == []
    with db.session() as s:
        with pytest.raises(HTTPException):
            other_release.owned(s, first["id"])
        assert s.scalar(select(func.count()).select_from(GameSave)) == 2


def test_old_frozen_recipe_can_create_new_scoped_save_after_schema_defaults_change(db, config, fake):
    config.product = "roleplay"
    app = create_app(config, db, fake, serve_web=False)
    with TestClient(app) as client:
        source = client.post(
            "/api/role/experiments/start",
            json={"request_id": "legacy-source", "template_id": "partial-hearing"},
        ).json()

    with db.session.begin() as s:
        origin = s.get(ExperimentInitial, source["story_id"])
        origin.recipe = {key: value for key, value in origin.recipe.items() if key != "video_assets"}
        origin.receipt = {**origin.receipt, "blueprint_id": "legacy-project"}

    async def ready():
        pass

    app.state.runtime.model_readiness.require = ready
    library = GameSaves(db, config, app.state.runtime, source_story_id=source["story_id"])
    created = asyncio.run(library.create(NewGame(request_id="legacy-play", label="旧故事新存档")))
    assert library.list()[0]["id"] == created["id"]

    from chatbot.experiment_initials import digest
    from chatbot.storage import Conversation

    with db.session() as s:
        source_recipe = s.get(ExperimentInitial, source["story_id"]).recipe
        save = s.get(GameSave, created["id"])
        player = s.get(Conversation, created["id"])
        clone_recipe = s.get(ExperimentInitial, player.story_id).recipe
        assert digest(source_recipe) != digest(clone_recipe)
        assert save.release_id == digest(source_recipe)
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location(
            "acceptance_scope", Path(__file__).resolve().parents[1] / "scripts/acceptance_scope.py"
        )
        scope = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scope)
        assert scope.belongs_to_project(s, player, "legacy-project")

    package_hash = "f" * 64
    with db.session.begin() as s:
        source_origin = s.get(ExperimentInitial, source["story_id"])
        source_origin.request_hash = package_hash
        source_origin.receipt = {
            **source_origin.receipt,
            "release_seed": True,
            "package_hash": package_hash,
        }
    with db.session() as s:
        player = s.get(Conversation, created["id"])
        assert scope.belongs_to_release(s, player, {"package_sha256": package_hash}, config.workspace_owner)
