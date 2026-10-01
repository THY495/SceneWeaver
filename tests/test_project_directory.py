import base64
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from chatbot.experiment_templates import template
from chatbot.project_directory import encoded, open_project, save_project


def package():
    recipe = template("ferry-narrative")
    return dict(format="sceneweaver.story.v2", recipe=recipe, assets={})


def test_cg_project_migrates_on_save_and_retains_prior_snapshot(tmp_path):
    root = tmp_path / "cg-project"
    original = package()
    first = save_project(root, original, expected_revision=0)
    old_snapshot = root / "content" / (first["content"] + ".json")
    original_bytes = old_snapshot.read_bytes()
    _, edited = open_project(root)
    # Authoring drafts can select CG before uploading the image.
    edited["recipe"]["narrative"]["nodes"][0]["visual_mode"] = "cg"
    second = save_project(root, edited, expected_revision=1)
    assert second["format"] == "sceneweaver.project.v3"
    assert old_snapshot.read_bytes() == original_bytes
    reopened, story = open_project(root)
    assert reopened["revision"] == 2
    assert story["format"] == "sceneweaver.story.v5"
    assert story["recipe"]["narrative"]["nodes"][0]["visual_mode"] == "cg"


def test_directory_move_and_copy_are_self_contained(tmp_path):
    source, moved, copied = (tmp_path / name for name in ("source", "moved", "copied"))
    original = package()
    png = Path("evals/studio-assets/test-pixel.png").read_bytes()
    key = hashlib.sha256(png).hexdigest()
    original["recipe"]["narrative"]["nodes"][0]["background"] = key
    original["assets"][key] = base64.b64encode(png).decode()
    manifest = save_project(source, original, expected_revision=0)
    shutil.move(source, moved)
    reopened, value = open_project(moved)
    assert reopened == manifest
    assert value["assets"] == original["assets"]
    second = save_project(copied, value, expected_revision=0)
    assert second["id"] != manifest["id"]
    value["recipe"]["title"] = "独立修改"
    save_project(copied, value, expected_revision=1)
    assert open_project(moved)[1]["recipe"]["title"] != "独立修改"


def test_portable_asset_order_gives_same_game_package_bytes(tmp_path):
    first = Path("evals/studio-assets/test-pixel.png").read_bytes()
    second = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9V8AAAAASUVORK5CYII="
    )
    assets = {hashlib.sha256(data).hexdigest(): base64.b64encode(data).decode() for data in (first, second)}
    original = package()
    original["recipe"]["image_assets"] = [
        dict(id=f"image-{index}", name=f"图片{index}", kind="png", content_hash=key)
        for index, key in enumerate(assets)
    ]
    keys = list(assets)
    left = {**original, "assets": {key: assets[key] for key in keys}}
    right = {**original, "assets": {key: assets[key] for key in reversed(keys)}}
    save_project(tmp_path / "left", left, expected_revision=0)
    save_project(tmp_path / "right", right, expected_revision=0)
    exported_left = open_project(tmp_path / "left")[1]
    exported_right = open_project(tmp_path / "right")[1]
    assert list(exported_left["assets"]) == sorted(keys)
    assert encoded(exported_left) == encoded(exported_right)
    assert encoded(left) == encoded(right)


def test_stale_revision_and_failed_commit_leave_previous_version(tmp_path, monkeypatch):
    root = tmp_path / "project"
    value = package()
    save_project(root, value, expected_revision=0)
    value["recipe"]["title"] = "second"
    save_project(root, value, expected_revision=1)
    with pytest.raises(ValueError, match="冲突"):
        save_project(root, package(), expected_revision=1)
    import chatbot.project_directory as module

    def fail(*args):
        raise OSError("simulated interrupted commit")

    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(OSError):
        save_project(root, package(), expected_revision=2)
    manifest, reopened = open_project(root)
    assert manifest["revision"] == 2 and reopened["recipe"]["title"] == "second"
    assert not (root / ".sceneweaver-write.lock").exists()


def test_missing_assets_invalid_version_and_tamper_are_rejected(tmp_path):
    root = tmp_path / "project"
    value = package()
    value["recipe"]["narrative"]["nodes"][0]["background"] = "a" * 64
    with pytest.raises(ValueError, match="图片"):
        save_project(root, value, expected_revision=0)
    assert not root.exists()
    manifest = save_project(root, package(), expected_revision=0)
    snapshot = root / "content" / (manifest["content"] + ".json")
    snapshot.write_text("{}")
    with pytest.raises(ValueError, match="损坏"):
        open_project(root)
    manifest["format"] = "future"
    (root / "sceneweaver.project.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="版本"):
        open_project(root)


def test_lock_and_existing_directory_are_not_overwritten(tmp_path):
    root = tmp_path / "project"
    save_project(root, package(), expected_revision=0)
    with pytest.raises(FileExistsError):
        save_project(root, package(), expected_revision=0)
    (root / ".sceneweaver-write.lock").write_text("other writer")
    with pytest.raises(ValueError, match="正在保存"):
        save_project(root, package(), expected_revision=1)
    assert open_project(root)[0]["revision"] == 1


def test_portable_documents_survive_without_database(tmp_path):
    value = package()
    value["format"] = "sceneweaver.story.v3"
    value["recipe"]["people"][0]["profile"]["document_ids"] = ["doc-1"]
    value["documents"] = {
        "doc-1": {
            "name": "背景资料",
            "source_version": 1,
            "parts": [{"section": "一", "content": "渡船停航"}],
        }
    }
    save_project(tmp_path / "project", value, expected_revision=0)
    assert open_project(tmp_path / "project")[1]["documents"] == value["documents"]


def test_manifest_cannot_select_arbitrary_file(tmp_path):
    root = tmp_path / "project"
    manifest = save_project(root, package(), expected_revision=0)
    manifest["content"] = "../../outside"
    (root / "sceneweaver.project.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="内容引用"):
        open_project(root)


def test_invalid_revision_does_not_create_directory(tmp_path):
    root = tmp_path / "project"
    with pytest.raises(ValueError, match="预期版本"):
        save_project(root, package(), expected_revision=-1)
    assert not root.exists()


def test_unused_named_asset_survives_directory_move(tmp_path):
    original = package()
    png = Path("evals/studio-assets/test-pixel.png").read_bytes()
    key = hashlib.sha256(png).hexdigest()
    original["assets"][key] = base64.b64encode(png).decode()
    original["recipe"]["image_assets"] = [
        dict(id="unused-picture", name="备用背景", kind="png", content_hash=key)
    ]
    source, moved = tmp_path / "source", tmp_path / "moved"
    save_project(source, original, expected_revision=0)
    shutil.move(source, moved)
    _, restored = open_project(moved)
    assert restored["recipe"]["image_assets"] == original["recipe"]["image_assets"]
    assert restored["assets"] == original["assets"]
    assert not any(n.get("background") for n in restored["recipe"]["narrative"]["nodes"])
    original["recipe"]["image_assets"].append(dict(original["recipe"]["image_assets"][0]))
    with pytest.raises(ValueError, match="重复"):
        save_project(tmp_path / "duplicate", original, expected_revision=0)
