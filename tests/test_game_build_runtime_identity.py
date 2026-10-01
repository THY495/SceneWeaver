"""A build request is bound to the exact game runtime copied for the player."""

import base64
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from chatbot import game_build
from chatbot.experiment_templates import template
from chatbot.project_directory import save_project
from chatbot.story_packages import MAX_ENCODED_PNG


def ready_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    save_project(
        project,
        {"format": "sceneweaver.story.v2", "recipe": template("ferry-narrative"), "assets": {}},
        expected_revision=0,
    )
    runtime = tmp_path / "runtime"
    for entry in ("Game/Game.exe", "GameBackend/GameBackend.exe", "pgsql/bin/postgres.exe"):
        path = runtime / entry
        path.parent.mkdir(parents=True)
        path.write_bytes(entry.encode())
    (runtime / "Game/LICENSE").write_bytes((Path(__file__).resolve().parents[1] / "LICENSE").read_bytes())
    return project, runtime


def test_same_request_rejects_changed_runtime_and_new_request_uses_it(tmp_path):
    project, runtime = ready_project(tmp_path)
    request = str(uuid4())
    first = game_build.build_project(project, runtime, 1, request)
    assert game_build.build_project(project, runtime, 1, request) == first
    (runtime / "Game/Game.exe").write_bytes(b"new signed runtime")
    with pytest.raises(ValueError, match="运行库已变化"):
        game_build.build_project(project, runtime, 1, request)
    second = game_build.build_project(project, runtime, 1, str(uuid4()))
    assert second["runtime_sha256"] != first["runtime_sha256"]
    assert (Path(second["path"]) / "Game.exe").read_bytes() == b"new signed runtime"
    assert (Path(second["path"]) / "LICENSE").read_bytes() == (runtime / "Game/LICENSE").read_bytes()
    assert game_build.list_builds(project)[0]["runtime_sha256"] == second["runtime_sha256"]


def test_retry_rejects_tampered_built_binary_or_legacy_receipt(tmp_path):
    project, runtime = ready_project(tmp_path)
    request = str(uuid4())
    result = game_build.build_project(project, runtime, 1, request)
    (Path(result["path"]) / "Game.exe").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="已有构建的游戏运行库已变化"):
        game_build.build_project(project, runtime, 1, request)
    receipt = project / "builds/.records" / (result["id"] + ".json")
    data = json.loads(receipt.read_text(encoding="utf-8"))
    del data["runtime_sha256"]
    receipt.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="旧构建缺少运行库校验"):
        game_build.build_project(project, runtime, 1, request)


def test_changed_bytes_during_copy_leave_incomplete_build(tmp_path, monkeypatch):
    project, runtime = ready_project(tmp_path)
    original = game_build.shutil.copytree

    def changed_copy(source, destination, *args, **kwargs):
        result = original(source, destination, *args, **kwargs)
        if Path(source).name == "Game":
            (Path(destination) / "Game.exe").write_bytes(b"changed during copy")
        return result

    monkeypatch.setattr(game_build.shutil, "copytree", changed_copy)
    with pytest.raises(ValueError, match="复制后校验不一致"):
        game_build.build_project(project, runtime, 1, str(uuid4()))
    assert game_build.list_builds(project)[0]["status"] == "incomplete"


def test_large_studio_background_requires_compatible_frozen_backend(tmp_path):
    image = (
        Path(__file__).resolve().parents[1]
        / "evals/studio-visual-demo/assets/archive-lobby-rain.png"
    ).read_bytes()
    assert 2 * 1024 * 1024 < len(image) < 4 * 1024 * 1024
    project, runtime = ready_project(tmp_path)
    recipe = template("ferry-narrative")
    key = hashlib.sha256(image).hexdigest()
    recipe["narrative"]["nodes"][0]["background"] = key
    recipe["image_assets"] = [
        {"id": str(uuid4()), "name": "archive-lobby-rain.png", "kind": "png", "content_hash": key}
    ]
    save_project(
        project,
        {
            "format": "sceneweaver.story.v2",
            "recipe": recipe,
            "assets": {key: base64.b64encode(image).decode("ascii")},
        },
        expected_revision=1,
    )
    with pytest.raises(ValueError, match="archive-lobby-rain.png.*换用更小的图片"):
        game_build.build_project(project, runtime, 2)
    assert game_build.list_builds(project) == []

    capability = runtime / "GameBackend" / game_build.CAPABILITIES_FILE
    capability.write_text(
        json.dumps(
            {
                "format": "sceneweaver.game-runtime-capabilities.v1",
                "max_png_bytes": 4 * 1024 * 1024,
                "max_encoded_png_chars": MAX_ENCODED_PNG,
            }
        ),
        encoding="utf-8",
    )
    built = game_build.build_project(project, runtime, 2)
    assert (Path(built["path"]) / "story.json").is_file()
    assert (Path(built["path"]) / "GameBackend" / game_build.CAPABILITIES_FILE).is_file()


def test_invalid_runtime_capability_manifest_fails_closed(tmp_path):
    project, runtime = ready_project(tmp_path)
    capability = runtime / "GameBackend" / game_build.CAPABILITIES_FILE
    capability.write_text('{"format": "unexpected"}', encoding="utf-8")
    with pytest.raises(ValueError, match="图片能力清单无效"):
        game_build.build_project(project, runtime, 1)
    assert game_build.list_builds(project) == []
