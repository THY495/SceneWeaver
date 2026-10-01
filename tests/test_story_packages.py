import base64
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.knowledge import Knowledge
from chatbot.storage import Chunk, Conversation, Document

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9V8AAAAASUVORK5CYII="
)


def upload_package(client, package):
    return client.post(
        "/api/role/packages/import", files={"file": ("story.json", json.dumps(package).encode())}
    )


@pytest.mark.parametrize("visual_mode", ["stage", "cg"])
def test_portable_package_fresh_assets_start_and_projection(db, config, fake, visual_mode):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        recipe = client.get("/api/role/experiments/templates/ferry-narrative").json()
        key = client.post("/api/role/presentation/sprites", files={"file": ("x.png", PNG)}).json()["id"]
        recipe["people"][0]["presentation"] = {"base_sprite": key}
        recipe["narrative"]["nodes"][0]["background"] = key
        if visual_mode == "cg":
            recipe["narrative"]["nodes"][0]["visual_mode"] = "cg"
        exported = client.post("/api/role/packages/export", json=recipe)
        assert exported.status_code == 200, exported.text
        package = exported.json()
        if visual_mode == "cg":
            assert package["format"] == "sceneweaver.story.v5"
            assert package["documents"] == {}
        assert list(package["assets"]) == [key]  # shared background/sprite stored once
        config.data_dir = config.data_dir / "fresh-device"
        assert client.get("/api/role/presentation/sprites/" + key).status_code == 404
        imported = upload_package(client, package)
        assert imported.status_code == 200, imported.text
        assert imported.json()["asset_count"] == 1
        assert client.get("/api/role/presentation/sprites/" + key).status_code == 200
        saved = client.put(
            "/api/role/experiments/blueprints/e162bda7-66ad-4cc8-8885-aa99dc6f6d10",
            json={"expected_version": 0, "recipe": imported.json()["recipe"]},
        )
        assert saved.status_code == 200, saved.text
        run = client.post(
            "/api/role/experiments/start",
            json={
                "request_id": "portable-start",
                "blueprint_id": "e162bda7-66ad-4cc8-8885-aa99dc6f6d10",
                "blueprint_version": saved.json()["version"],
            },
        )
        assert run.status_code == 201, run.text
        sid = run.json()["story_id"]
        frame = client.get(f"/api/role/experiments/stories/{sid}/narrative").json()
        assert frame["background"] == key
        assert frame["visual_mode"] == visual_mode
        assert "nodes" not in frame
        assert client.get(f"/api/role/stories/{sid}/settings").json()["paused"]


def test_package_rejects_missing_extra_corrupt_and_legacy_assets(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        recipe = client.get("/api/role/experiments/templates/ferry-narrative").json()
        legacy = {"format": "sceneweaver.story.v1", "recipe": recipe}
        assert upload_package(client, legacy).status_code == 200
        recipe["narrative"]["nodes"][0]["background"] = "a" * 64
        assert upload_package(client, legacy).status_code == 422
        assert client.post("/api/role/packages/export", json=recipe).status_code == 422
        package = {**legacy, "format": "sceneweaver.story.v2", "assets": {}}
        assert upload_package(client, package).status_code == 422
        package["assets"] = {"a" * 64: base64.b64encode(PNG).decode()}
        assert upload_package(client, package).status_code == 422  # hash mismatch
        package["assets"] = {"../../escape": "bad"}
        assert upload_package(client, package).status_code == 422
        assert not (config.data_dir / "sprites").exists()
        invalid = copy.deepcopy(legacy)
        invalid["recipe"]["people"][0]["profile"]["document_ids"] = ["foreign-document"]
        assert upload_package(client, invalid).status_code == 422
        assert upload_package(client, {"format": "unknown"}).status_code == 422


def test_portable_package_accepts_studio_sized_png(db, config, fake):
    """A Studio-approved background must remain loadable from its released package."""
    config.product = "roleplay"
    image = (
        Path(__file__).resolve().parents[1]
        / "evals/studio-visual-demo/assets/archive-lobby-rain.png"
    ).read_bytes()
    assert 2_100_000 < len(image) < 4 * 1024 * 1024
    with TestClient(create_app(config, db, fake)) as client:
        recipe = client.get("/api/role/experiments/templates/ferry-narrative").json()
        key = client.post(
            "/api/role/presentation/sprites", files={"file": ("lobby.png", image)}
        ).json()["id"]
        recipe["narrative"]["nodes"][0]["background"] = key
        exported = client.post("/api/role/packages/export", json=recipe)
        assert exported.status_code == 200, exported.text
        config.data_dir = config.data_dir / "fresh-device"
        imported = upload_package(client, exported.json())
        assert imported.status_code == 200, imported.text
        data_url = client.get("/api/role/presentation/sprites/" + key).json()["data_url"]
        assert base64.b64decode(data_url.split(",", 1)[1]) == image


def test_document_snapshots_remap_preserve_scope_and_search(db, config, fake):
    config.product = "roleplay"
    source_id = "f5f8f2de-5b62-4916-bd0d-73c6f558cb02"
    with db.session.begin() as s:
        s.add(
            Document(
                id=source_id,
                owner_id=config.workspace_owner,
                name="秘密.md",
                version=4,
                content_hash="a" * 64,
                blob_path="unused-original",
                status="ready",
            )
        )
        s.flush()
        s.add(
            Chunk(
                document_id=source_id,
                version=4,
                ordinal=0,
                section="旧信",
                content="密钥代号是蓝鹭。",
                terms="蓝鹭",
            )
        )
    with TestClient(create_app(config, db, fake)) as client:
        recipe = client.get("/api/role/experiments/templates/ferry-narrative").json()
        recipe["people"][0]["profile"]["document_ids"] = [source_id]
        exported = client.post("/api/role/packages/export", json=recipe)
        assert exported.status_code == 200, exported.text
        package = exported.json()
        assert package["format"] == "sceneweaver.story.v3"
        assert source_id not in json.dumps(package)
        assert "unused-original" not in json.dumps(package)
        # Make source inaccessible before import: only package content may be used.
        with db.session.begin() as s:
            s.get(Document, source_id).owner_id = "another-workspace"
        imported = upload_package(client, package)
        assert imported.status_code == 200, imported.text
        value = imported.json()
        assert value["document_count"] == 1 and value["document_index"] == "lexical"
        restored = value["recipe"]
        new_id = restored["people"][0]["profile"]["document_ids"][0]
        assert new_id != source_id
        assert restored["people"][1]["profile"]["document_ids"] == []
        bid = "ca6b4428-d813-4010-aa81-3a4bf77cc911"
        saved = client.put(
            f"/api/role/experiments/blueprints/{bid}", json={"expected_version": 0, "recipe": restored}
        )
        assert saved.status_code == 200, saved.text
        run = client.post(
            "/api/role/experiments/start",
            json={
                "request_id": "docs-start",
                "blueprint_id": bid,
                "blueprint_version": saved.json()["version"],
            },
        )
        assert run.status_code == 201, run.text
        knowledge = Knowledge(db, config, None, fake)
        with db.session() as s:
            people = [s.get(Conversation, run.json()["actors"][p["alias"]]) for p in restored["people"]]
            scope = [p.current_state["profile"]["document_ids"] for p in people]
            imported_doc = s.get(Document, new_id)
            assert imported_doc.owner_id == config.workspace_owner
            assert imported_doc.version == 1
            assert all(
                c.embedding is None for c in s.scalars(select(Chunk).where(Chunk.document_id == new_id))
            )
        assert knowledge.lexical_search(config.workspace_owner, "蓝鹭", document_ids=scope[0])
        assert not knowledge.lexical_search(config.workspace_owner, "蓝鹭", document_ids=scope[1])
        bad = copy.deepcopy(package)
        bad["documents"] = {}
        with db.session() as s:
            before = len(list(s.scalars(select(Document.id))))
        assert upload_package(client, bad).status_code == 422
        with db.session() as s:
            assert len(list(s.scalars(select(Document.id)))) == before
