import hashlib
import json
import shutil
import struct
from pathlib import Path

import pytest
from fastapi import HTTPException

from chatbot.experiment_templates import template
from chatbot.game_build import build_project
from chatbot.game_seed import load_game_seed
from chatbot.project_directory import open_project, save_project
from chatbot.release_manifest import load_release
from chatbot.video_assets import video_dir


def setup_project(tmp_path):
    def box(kind, body):
        return struct.pack(">I4s", len(body) + 8, kind) + body

    data = box(b"ftyp", b"isom0000mp42") + box(b"moov", b"track") + box(b"mdat", b"data")
    key = hashlib.sha256(data).hexdigest()
    source = tmp_path / "source"
    source.mkdir()
    (source / (key + ".mp4")).write_bytes(data)
    recipe = template("ferry-narrative")
    recipe["video_assets"] = [{"id": "intro", "name": "序幕", "content_hash": key, "bytes": len(data)}]
    package = {"format": "sceneweaver.story.v5", "recipe": recipe, "assets": {}, "documents": {}}
    project = tmp_path / "project"
    save_project(project, package, expected_revision=0, video_source=source)
    runtime = tmp_path / "runtime"
    for entry in ("Game/Game.exe", "GameBackend/GameBackend.exe", "pgsql/bin/postgres.exe"):
        path = runtime / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"layout-test-only")
    return project, runtime, key, data


def test_project_move_and_game_build_carry_verified_sidecars(tmp_path):
    project, runtime, key, data = setup_project(tmp_path)
    moved = tmp_path / "moved"
    shutil.move(project, moved)
    assert open_project(moved)[1]["format"] == "sceneweaver.story.v5"
    build = build_project(moved, runtime, 1)
    release = Path(build["path"])
    assert (release / "videos" / (key + ".mp4")).read_bytes() == data
    assert load_release(release)
    (release / "videos" / (key + ".mp4")).write_bytes(data[:-1])
    with pytest.raises(ValueError, match="长度"):
        load_release(release)
    # Damage to a release doesn't affect the source project.
    assert open_project(moved)[0]["revision"] == 1


@pytest.mark.asyncio
async def test_seed_import_restores_cache_and_revalidates_sidecars(db, config, tmp_path):
    config.product = "roleplay"
    project, runtime, key, data = setup_project(tmp_path)
    release = Path(build_project(project, runtime, 1)["path"])
    seed = await load_game_seed(db, config, release / "story.json")
    cache = video_dir(config, config.workspace_owner) / (key + ".mp4")
    assert cache.read_bytes() == data
    cache.unlink()
    assert await load_game_seed(db, config, release / "story.json") == seed
    assert cache.read_bytes() == data
    (release / "videos" / (key + ".mp4")).unlink()
    with pytest.raises(HTTPException, match="视频"):
        await load_game_seed(db, config, release / "story.json")


def test_missing_video_cannot_commit_new_project_revision(tmp_path):
    project, _, key, _ = setup_project(tmp_path)
    first, package = open_project(project)
    (project / "videos" / (key + ".mp4")).unlink()
    with pytest.raises(ValueError, match="视频"):
        save_project(project, package, expected_revision=1)
    manifest = json.loads((project / "sceneweaver.project.json").read_text(encoding="utf-8"))
    assert manifest == first
