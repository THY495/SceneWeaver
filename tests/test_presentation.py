import base64

from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.schemas import ActorSpeech
from chatbot.storage import Conversation


def test_sprite_upload_validation_and_perceived_stage(db, config, fake):
    config.product = "roleplay"
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9V8AAAAASUVORK5CYII="
    )
    with TestClient(create_app(config, db, fake)) as client:
        root = "/api/role/presentation"
        upload = client.post(root + "/sprites", files={"file": ("sprite.png", png, "image/png")})
        assert upload.status_code == 201, upload.text
        key = upload.json()["id"]
        assert (
            client.get(root + "/sprites/" + key).json()["data_url"].endswith(base64.b64encode(png).decode())
        )
        assert client.post(root + "/sprites", files={"file": ("bad.png", b"<svg />")}).status_code == 422
        assert (
            client.post(
                root + "/sprites", files={"file": ("large.png", b"x" * (4 * 1024 * 1024 + 1))}
            ).status_code
            == 413
        )
        run = client.post(
            "/api/role/experiments/start", json={"request_id": "stage", "template_id": "partial-hearing"}
        ).json()
        # Player in the foyer must not see curator/restorer in the reading room.
        stage = client.get(root + "/stage/" + run["player_id"]).json()
        assert [c["name"] for c in stage["characters"]] == ["许遥"]
        with db.session.begin() as s:
            npc = s.get(Conversation, run["actors"]["reporter"])
            append_record(
                s,
                story_id=run["story_id"],
                kind="speech",
                actor=npc,
                text="你好，秘密编号是蓝鹭。",
                observer_ids=[npc.id, run["player_id"]],
                dedupe="partial-performance",
                perceptions={run["player_id"]: {"text": "你好", "speaker_known": False}},
                payload={
                    "performance": [
                        {
                            "text": "你好，秘密编号是蓝鹭。",
                            "expression": "happy",
                            "pose": "neutral",
                            "voice_direction": "秘密语气",
                        }
                    ]
                },
            )
        stage = client.get(root + "/stage/" + run["player_id"]).json()
        assert stage["last_speech"] == {"speaker_id": None, "text": "你好", "segments": []}
        assert "蓝鹭" not in str(stage)
        assert client.get(root + "/stage/" + run["actors"]["reporter"]).status_code == 404


def test_performance_contract_keeps_old_outputs_compatible():
    assert ActorSpeech.model_validate({"text": "你好"}).performance == []
    value = ActorSpeech.model_validate(
        {
            "text": "你好",
            "spoken_parts": ["你好"],
            "performance": [
                {"text": "你好", "expression": "concerned", "pose": "thinking", "voice_direction": "语速稍慢"}
            ],
        }
    )
    assert value.performance[0].expression == "concerned"


def test_unstaged_cast_follows_authored_order_across_new_runs(db, config, fake):
    from uuid import uuid4

    from chatbot.experiment_templates import template

    config.product = "roleplay"
    recipe = template("partial-hearing")
    for person in recipe["people"]:
        person["location"] = recipe["player"]["location"]
    recipe["narrative"] = {
        "entry": "a",
        "nodes": [{"id": "a", "title": "集合", "text": "三人在这里。", "observers": ["player"]}],
        "edges": [],
    }
    with TestClient(create_app(config, db, fake)) as client:
        blueprint_id = str(uuid4())
        saved = client.put(
            f"/api/role/experiments/blueprints/{blueprint_id}",
            json={"expected_version": 0, "recipe": recipe},
        )
        assert saved.status_code == 200, saved.text
        for index in range(2):
            run = client.post(
                "/api/role/experiments/start",
                json={
                    "request_id": f"unstaged-order-{index}",
                    "blueprint_id": blueprint_id,
                    "blueprint_version": 1,
                },
            ).json()
            stage = client.get(f"/api/role/presentation/stage/{run['player_id']}").json()
            assert [person["name"] for person in stage["characters"]] == [
                person["profile"]["name"] for person in recipe["people"]
            ]


def test_authored_stage_never_widens_perception_and_respects_empty_and_legacy(db, config, fake):
    from uuid import uuid4

    from chatbot.experiment_templates import template

    config.product = "roleplay"
    recipe = template("partial-hearing")
    recipe["narrative"] = {
        "entry": "a",
        "nodes": [
            {
                "id": "a",
                "title": "布置",
                "objects": [{"id": "decor", "kind": "decoration", "name": "灯", "sprite": "a" * 64, "transform": {"x": 800, "y": 810}, "editor_hidden": True}],
                "text": "雨声响起。",
                "observers": ["player"],
                "stage": [
                    {"actor": "curator", "position": "left"},
                    {"actor": "reporter", "position": "right", "editor_hidden": True, "editor_locked": True, "transform": {"version": 1, "x": 1000, "y": 810, "rotation": 15}},
                ],
            },
            {"id": "b", "title": "空镜", "text": "雨声未停。", "observers": ["player"], "stage": []},
            {"id": "c", "title": "默认", "text": "雨渐小。", "observers": ["player"]},
        ],
        "edges": [
            {"id": "ab", "source": "a", "target": "b", "label": "继续"},
            {"id": "bc", "source": "b", "target": "c", "label": "继续"},
        ],
    }
    with TestClient(create_app(config, db, fake)) as client:
        bid = str(uuid4())
        base = "/api/role/experiments"
        saved = client.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe})
        assert saved.status_code == 200, saved.text
        run = client.post(
            base + "/start", json={"request_id": "stage-layout", "blueprint_id": bid, "blueprint_version": 1}
        ).json()
        path = "/api/role/presentation/stage/" + run["player_id"]
        characters = client.get(path).json()["characters"]
        assert [(c["name"], c["position"]) for c in characters] == [("许遥", "right")]
        assert characters[0]["transform"]["x"] == 1000
        assert characters[0]["transform"]["rotation"] == 15
        assert characters[0]["draw_order"] == 1  # Hidden predecessor must not renumber the visible actor.
        assert "editor_hidden" not in characters[0]
        assert "editor_locked" not in characters[0]
        snapshot = client.get(f"/api/role/players/{run['player_id']}/snapshot").json()
        assert snapshot["objects"][0]["id"] == "decor"
        assert "editor_hidden" not in snapshot["objects"][0]
        # Author edits cannot mutate the frozen run.
        recipe["narrative"]["nodes"][0]["stage"] = []
        assert (
            client.put(
                base + "/blueprints/" + bid, json={"expected_version": 1, "recipe": recipe}
            ).status_code
            == 200
        )
        assert client.get(path).json()["characters"] == characters
        settings_path = f"/api/role/stories/{run['story_id']}/settings"
        settings = client.get(settings_path).json()
        client.put(settings_path, json={**settings, "paused": False})
        for revision, edge in [(1, "ab"), (2, "bc")]:
            result = client.post(
                base + f"/stories/{run['story_id']}/narrative/choose",
                json={"request_id": edge, "edge_id": edge, "expected_revision": revision},
            )
            assert result.status_code == 200, result.text
            actual = client.get(path).json()["characters"]
            if edge == "ab":
                assert actual == []
            else:
                assert [c["name"] for c in actual] == ["许遥"]
                assert "position" not in actual[0]
        with db.session() as s:
            curator = s.get(Conversation, run["actors"]["curator"])
            assert curator.current_state["location"] == recipe["people"][0]["location"]
