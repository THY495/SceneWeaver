import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.project_directory import open_project
from chatbot.project_workspace import project_root
from chatbot.storage import Chunk, Document, StoryBlueprint


def test_interrupted_build_is_not_reported_complete(tmp_path, monkeypatch):
    from chatbot import game_build
    from chatbot.project_directory import save_project

    package = dict(format="sceneweaver.story.v2", recipe=template("ferry-narrative"), assets={})
    project = tmp_path / "project"
    save_project(project, package, expected_revision=0)
    runtime = tmp_path / "runtime"
    for entry in ("Game/Game.exe", "GameBackend/GameBackend.exe", "pgsql/bin/postgres.exe"):
        path = runtime / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"layout-only")
    original = game_build.shutil.copytree
    calls = 0

    def interrupted(source, destination, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interrupted copy")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(game_build.shutil, "copytree", interrupted)
    request_id = str(uuid4())
    with pytest.raises(OSError):
        game_build.build_project(project, runtime, 1, request_id)
    assert game_build.list_builds(project)[0]["status"] == "incomplete"
    with pytest.raises(ValueError, match="未完成"):
        game_build.build_project(project, runtime, 1, request_id)
    assert open_project(project)[0]["revision"] == 1


def test_backstage_draft_cannot_be_built_as_a_player_game(tmp_path):
    from chatbot.game_build import build_project
    from chatbot.project_directory import save_project

    recipe = template("ferry-narrative")
    recipe["player"]["location"] = "门厅"
    opening = recipe["narrative"]["nodes"][0]
    opening["mode"] = "backstage"
    opening["participants"] = ["captain", "keeper"]
    opening["observers"] = ["captain", "keeper"]
    project = tmp_path / "project"
    save_project(
        project,
        {"format": "sceneweaver.story.v2", "recipe": recipe, "assets": {}},
        expected_revision=0,
    )
    with pytest.raises(ValueError, match="尚不能在独立游戏中自动运行"):
        build_project(project, tmp_path / "runtime", 1)
    assert not (project / "builds" / ".records").exists()


def test_build_saved_project_is_versioned_and_uses_host_runtime(db, config, fake, tmp_path):
    from chatbot.game_build import filesystem_path

    config.product = "roleplay"
    pid = str(uuid4())
    runtime = tmp_path / "runtime"
    for entry in ("Game/Game.exe", "GameBackend/GameBackend.exe", "pgsql/bin/postgres.exe"):
        path = runtime / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"layout-test-only")
    nested = Path("_internal") / ("dependency-" + "x" * 65) / ("licenses-" + "y" * 65) / "LICENSE.txt"
    long_source = filesystem_path(runtime / "Game" / nested)
    long_source.parent.mkdir(parents=True)
    long_source.write_text("license fixture", encoding="utf-8")
    with TestClient(create_app(config, db, fake)) as client:
        url = "/api/role/projects/" + pid
        assert (
            client.put(url, json=dict(expected_version=0, recipe=template("ferry-narrative"))).status_code
            == 200
        )
        assert client.post(url + "/builds", json={"expected_version": 1}).status_code == 503
        config.windows_game_runtime_dir = runtime
        assert client.post(url + "/builds", json={"expected_version": 2}).status_code == 409
        build_request = {"expected_version": 1, "request_id": str(uuid4())}
        response = client.post(url + "/builds", json=build_request)
        assert response.status_code == 201, response.text
        assert client.post(url + "/builds", json=build_request).json() == response.json()
        history = client.get(url + "/builds").json()
        assert len(history) == 1 and history[0]["status"] == "completed"
        build = Path(response.json()["path"])
        assert build.is_relative_to(project_root(config, config.workspace_owner) / pid / "builds")
        assert json.loads((build / "game.json").read_text(encoding="utf-8"))["id"] == pid
        assert (build / "Game.exe").is_file()
        assert filesystem_path(build / nested).read_text(encoding="utf-8") == "license fixture"
        assert not (build / "sceneweaver.project.json").exists()
        assert not (build / "content").exists()
        (build / "story.json").write_bytes(b"corrupt")
        assert client.post(url + "/builds", json=build_request).status_code == 422
        assert client.get(url + "/builds").json()[0]["status"] == "damaged"
        assert (
            client.post(url + "/builds", json={"expected_version": 1, "runtime": "outside"}).status_code
            == 422
        )


def test_disk_workspace_save_retry_cache_rebuild_and_preview(db, config, fake):
    config.product = "roleplay"
    pid = str(uuid4())
    recipe = template("ferry-narrative")
    body = dict(expected_version=0, recipe=recipe)
    with TestClient(create_app(config, db, fake)) as client:
        url = "/api/role/projects/" + pid
        result = client.put(url, json=body)
        assert result.status_code == 200, result.text
        assert result.json()["version"] == 1
        assert client.put(url, json=body).json() == result.json()
        assert client.get("/api/role/projects").json()[0]["id"] == pid
        root = project_root(config, config.workspace_owner) / pid
        assert open_project(root)[0]["revision"] == 1
        old_url = "/api/role/experiments/blueprints/" + pid
        assert client.put(old_url, json={**body, "expected_version": 1}).status_code == 409
        changed = {**recipe, "title": "磁盘新版"}
        assert client.put(url, json={**body, "recipe": changed}).status_code == 409
        assert client.put(url, json=dict(expected_version=1, recipe=changed)).status_code == 200
        with db.session.begin() as s:
            s.delete(s.get(StoryBlueprint, pid))
        reopened = client.get(url)
        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["recipe"]["title"] == "磁盘新版"
        assert reopened.json()["version"] == 2
        run = client.post(
            "/api/role/experiments/start",
            json=dict(request_id="disk-preview", blueprint_id=pid, blueprint_version=2),
        )
        assert run.status_code == 201, run.text
        assert open_project(root)[0]["revision"] == 2


def test_legacy_cannot_be_adopted_and_broken_project_stays_visible(db, config, fake):
    config.product = "roleplay"
    pid = str(uuid4())
    body = dict(expected_version=0, recipe=template("ferry-narrative"))
    with TestClient(create_app(config, db, fake)) as client:
        assert client.put("/api/role/experiments/blueprints/" + pid, json=body).status_code == 200
        assert client.put("/api/role/projects/" + pid, json=body).status_code == 409
        other = str(uuid4())
        assert client.put("/api/role/projects/" + other, json=body).status_code == 200
        root = project_root(config, config.workspace_owner) / other
        (root / "sceneweaver.project.json").write_text("{}")
        listed = client.get("/api/role/projects").json()
        assert listed[0]["id"] == other and listed[0]["error"]
        assert client.get("/api/role/projects/" + other).status_code == 422


def test_document_snapshot_names_remain_stable_across_saves(db, config, fake):
    config.product = "roleplay"
    source = str(uuid4())
    with db.session.begin() as s:
        s.add(
            Document(
                id=source,
                owner_id=config.workspace_owner,
                name="角色资料.md",
                version=1,
                content_hash="a" * 64,
                blob_path="unused",
                status="ready",
            )
        )
        s.flush()
        s.add(
            Chunk(
                document_id=source,
                version=1,
                ordinal=0,
                section="背景",
                content="停航消息只有船长知道。",
                terms="停航",
            )
        )
    recipe = template("ferry-narrative")
    recipe["people"][0]["profile"]["document_ids"] = [source]
    pid = str(uuid4())
    with TestClient(create_app(config, db, fake)) as client:
        url = "/api/role/projects/" + pid
        first = client.put(url, json=dict(expected_version=0, recipe=recipe))
        assert first.status_code == 200, first.text
        next_recipe = first.json()["recipe"]
        next_recipe["title"] = "再保存"
        second = client.put(url, json=dict(expected_version=1, recipe=next_recipe))
        assert second.status_code == 200, second.text
        root = project_root(config, config.workspace_owner) / pid
        assert list(open_project(root)[1]["documents"].values())[0]["name"] == "角色资料.md"


def test_unused_asset_package_round_trip(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        uploaded = client.post(
            "/api/role/presentation/sprites",
            files={
                "file": ("spare.png", Path("evals/studio-assets/test-pixel.png").read_bytes(), "image/png")
            },
        )
        assert uploaded.status_code == 201, uploaded.text
        key = uploaded.json()["id"]
        recipe = template("ferry-narrative")
        recipe["image_assets"] = [dict(id="spare", name="备用图片", kind="png", content_hash=key)]
        pid = str(uuid4())
        saved = client.put("/api/role/projects/" + pid, json=dict(expected_version=0, recipe=recipe))
        assert saved.status_code == 200, saved.text
        exported = client.post("/api/role/packages/export", json=saved.json()["recipe"])
        assert exported.status_code == 200, exported.text
        assert key in exported.json()["assets"]
        imported = client.post(
            "/api/role/packages/import",
            files={"file": ("story.json", json.dumps(exported.json()).encode(), "application/json")},
        )
        assert imported.status_code == 200, imported.text
        assert imported.json()["recipe"]["image_assets"] == recipe["image_assets"]
