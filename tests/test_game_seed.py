import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from chatbot.api import create_app
from chatbot.game_saves import GameSaves, NewGame
from chatbot.game_seed import load_game_seed
from chatbot.storage import Conversation, ExperimentInitial, Story, Task


async def test_exported_package_becomes_stable_seed_and_fresh_player_save(
    db, config, fake, runtime, tmp_path
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as studio:
        recipe = studio.get("/api/role/experiments/templates/ferry-narrative").json()
        response = studio.post("/api/role/packages/export", json=recipe)
        assert response.status_code == 200
    package = tmp_path / "story.json"
    package.write_text(json.dumps(response.json()), encoding="utf-8")
    seed = await load_game_seed(db, config, package)
    assert await load_game_seed(db, config, package) == seed
    import os

    if os.name == "nt":
        from pydantic import SecretStr

        from chatbot.native_game import create_native_game

        config.access_token = SecretStr("synthetic-native-token")
        web = tmp_path / "game-web"
        web.mkdir()
        (web / "game.html").write_text("Independent game", encoding="utf-8")
        (web / "assets").mkdir()
        app, worker = await create_native_game(
            config,
            db,
            package_path=package,
            web_dir=web,
            credentials_path=tmp_path / "player.dpapi",
        )
        assert worker.runtime.execution_scope.release_id == app.state.game_saves.release_id
        with db.session() as s:
            assert seed not in s.scalars(worker.runtime.execution_scope.stories()).all()
        with TestClient(app) as game:
            headers = {"Authorization": "Bearer synthetic-native-token"}
            assert game.get("/").text == "Independent game"
            assert game.get("/api/player/credentials", headers=headers).status_code == 200
            assert game.get("/api/role/experiments/blueprints", headers=headers).status_code == 404
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Conversation)) == 0
        assert s.scalar(
            select(func.count()).select_from(Task).where(Task.kind != "connection_check")
        ) == 0
        assert s.get(Story, seed).settings["paused"] is True
    runtime.model_readiness = SimpleNamespace(require=AsyncMock())
    saves = GameSaves(db, config, runtime, source_story_id=seed)
    created = await saves.create(NewGame(request_id="player", label="旅程"))
    assert saves.list()[0]["id"] == created["id"]
    assert await load_game_seed(db, config, package) == seed
    with db.session.begin() as s:
        import hashlib
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location("acceptance_scope", Path("scripts/acceptance_scope.py"))
        scope = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scope)
        release = {"package_sha256": hashlib.sha256(package.read_bytes()).hexdigest()}
        player = s.get(Conversation, created["id"])
        assert scope.belongs_to_release(s, player, release, config.workspace_owner)
        assert not scope.belongs_to_release(s, player, release, "other-owner")
        assert not scope.belongs_to_release(s, player, {"package_sha256": "0" * 64}, config.workspace_owner)
        initial = s.get(ExperimentInitial, seed)
        initial.recipe = {**initial.recipe, "title": "tampered"}
        assert not scope.belongs_to_release(s, player, release, config.workspace_owner)
    with pytest.raises(HTTPException) as error:
        await load_game_seed(db, config, package)
    assert error.value.status_code == 409


async def test_invalid_package_creates_no_seed(db, config, tmp_path):
    package = tmp_path / "bad.json"
    package.write_text('{"format":"unknown"}', encoding="utf-8")
    with pytest.raises(HTTPException):
        await load_game_seed(db, config, package)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Story)) == 0
