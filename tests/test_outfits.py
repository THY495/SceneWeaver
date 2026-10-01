from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.observations import append_record, observed_record, record_data
from chatbot.presentation import resolve_sprite
from chatbot.storage import Conversation
from chatbot.story_blueprints import Presentation, Recipe
from chatbot.story_packages import references


def wardrobe():
    return {"base_sprite": "a" * 64, "default_outfit": "casual", "outfits": [
        {"id": "casual", "name": "常服", "default_variant": "normal", "variants": [
            {"id": "normal", "name": "常态", "sprite": "b" * 64},
            {"id": "smile", "name": "微笑", "sprite": "c" * 64, "expression": "happy"},
        ]},
        {"id": "coat", "name": "雨衣", "default_variant": "normal", "variants": [
            {"id": "normal", "name": "常态", "sprite": "d" * 64},
        ]},
    ]}


def test_outfit_contract_legacy_fallback_and_asset_closure():
    old = {"base_sprite": "a" * 64}
    assert resolve_sprite(old)["base_sprite"] == "a" * 64
    assert old == {"base_sprite": "a" * 64}  # Reading never rewrites legacy input.
    config = wardrobe()
    parsed = Presentation.model_validate(config)
    assert parsed.version == 1
    assert resolve_sprite(config)["base_sprite"] == "b" * 64
    assert resolve_sprite(config, "casual", "smile")["base_sprite"] == "c" * 64
    assert resolve_sprite(config, "coat", "missing")["base_sprite"] == "d" * 64
    assert {v["sprite"] for v in resolve_sprite(config, "coat")["variants"]} == {"d" * 64}
    recipe = template("partial-hearing")
    recipe["people"][0]["presentation"] = config
    assert set("abcd") == {key[0] for key in references(Recipe.model_validate(recipe))}


def test_outfit_rejects_unknown_version_duplicates_and_dangling_selection():
    for changes in [{"version": 2}, {"default_outfit": "missing"}, {"outfits": [wardrobe()["outfits"][0]] * 2}]:
        with pytest.raises(ValueError):
            Presentation.model_validate({**wardrobe(), **changes})
    recipe = template("ferry-narrative")
    alias = recipe["people"][0]["alias"]
    recipe["people"][0]["presentation"] = wardrobe()
    node = recipe["narrative"]["nodes"][0]
    node["stage"] = [{"actor": alias, "outfit": "coat", "variant": "smile"}]
    with pytest.raises(ValueError):
        Recipe.model_validate(recipe)
    node["stage"][0]["variant"] = "normal"
    Recipe.model_validate(recipe)


def test_frozen_appearance_follows_opening_not_future_and_survives_history(db, config, fake):
    config.product = "roleplay"
    recipe = template("partial-hearing")
    next(p for p in recipe["people"] if p["alias"] == "reporter")["presentation"] = wardrobe()
    recipe["narrative"] = {"entry": "a", "nodes": [{
        "id": "a", "title": "雨夜", "observers": ["reporter", "player"],
        "stage": [{"actor": "reporter", "outfit": "casual", "draw_order": 7}],
        "opening": [
            {"id": "hello", "kind": "dialogue", "speaker": "reporter", "text": "你好。", "variant": "smile"},
            {"id": "coat", "kind": "dialogue", "speaker": "reporter", "text": "我穿上雨衣。", "outfit": "coat"},
        ],
    }], "edges": []}
    with TestClient(create_app(config, db, fake)) as client:
        bid = str(uuid4())
        result = client.put(f"/api/role/experiments/blueprints/{bid}", json={"expected_version": 0, "recipe": recipe})
        assert result.status_code == 200, result.text
        result = client.post("/api/role/experiments/start", json={"request_id": "outfits", "blueprint_id": bid, "blueprint_version": 1})
        assert result.status_code == 201, result.text
        run = result.json()
        path = f"/api/role/players/{run['player_id']}/snapshot"
        first = client.get(path).json()
        speech = next(e for e in first["events"] if e["text"] == "你好。")
        assert speech["appearance"]["sprite"] == "c" * 64
        assert first["characters"][0]["base_sprite"] == "c" * 64
        assert first["characters"][0]["draw_order"] == 7
        assert "d" * 64 not in str(first)
        settings_path = f"/api/role/stories/{run['story_id']}/settings"
        settings = client.get(settings_path).json()
        client.put(settings_path, json={**settings, "paused": False})
        advanced = client.post(f"/api/role/experiments/stories/{run['story_id']}/narrative/advance", json={"request_id": "next", "node_id": "a", "expected_version": 1})
        assert advanced.status_code == 200, advanced.text
        second = client.get(path).json()
        assert second["characters"][0]["base_sprite"] == "d" * 64
        assert next(e for e in second["events"] if e["text"] == "你好。")["appearance"] == speech["appearance"]
        with db.session.begin() as s:
            npc = s.get(Conversation, run["actors"]["reporter"])
            record = append_record(s, story_id=run["story_id"], kind="speech", actor=npc,
                text="很高兴。", observer_ids=[npc.id, run["player_id"]], dedupe="mood",
                payload={"performance": [{"text": "很高兴。", "expression": "happy", "pose": "neutral"}]})
            assert record.payload["appearance"]["outfit"] == "coat"
            assert record.payload["performance"][0]["appearance"]["sprite"] == "d" * 64
            hidden = append_record(s, story_id=run["story_id"], kind="speech", actor=npc,
                text="你好，秘密。", observer_ids=[npc.id, run["player_id"]], dedupe="hidden",
                perceptions={run["player_id"]: {"text": "你好", "speaker_known": False}})
            projection = record_data(observed_record(s, run["player_id"], hidden.id), viewer_id=run["player_id"])
            assert "appearance" not in projection
            # A long subsequent archive cannot cause the authored outfit to revert.
            for i in range(102):
                append_record(s, story_id=run["story_id"], kind="host", text="雨声", observer_ids=[run["player_id"]], dedupe=f"rain:{i}")
        assert client.get(path).json()["characters"][0]["base_sprite"] == "d" * 64
