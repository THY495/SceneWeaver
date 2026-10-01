"""Deterministic integration acceptance, never evidence of real-model quality."""

import base64
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.auto_routing import assess
from chatbot.project_directory import open_project
from chatbot.project_workspace import project_root
from chatbot.storage import StoryBlueprint, Task


async def test_two_scene_authoring_playthrough_with_mock_routing(db, config, runtime, fake):
    config.product = "roleplay"
    package = json.loads(Path("evals/studio-three-part/story.sceneweaver.json").read_text(encoding="utf-8"))
    pid = str(uuid4())
    with TestClient(create_app(config, db, fake)) as client:
        for key, data in package["assets"].items():
            uploaded = client.post(
                "/api/role/presentation/sprites",
                files={"file": (key + ".png", base64.b64decode(data), "image/png")},
            )
            assert uploaded.status_code == 201, uploaded.text
            assert uploaded.json()["id"] == key
        recipe = deepcopy(package["recipe"])
        recipe["narrative"]["edges"] = []
        project_url = "/api/role/projects/" + pid
        saved = client.put(project_url, json=dict(expected_version=0, recipe=recipe))
        assert saved.status_code == 200, saved.text
        with db.session.begin() as s:
            s.delete(s.get(StoryBlueprint, pid))
        reopened = client.get(project_url)
        assert reopened.status_code == 200, reopened.text
        assert len(reopened.json()["recipe"]["narrative"]["nodes"]) == 2
        assert (
            client.post(
                "/api/role/experiments/start",
                json=dict(request_id="play", blueprint_id=pid, blueprint_version=1),
            ).status_code
            == 422
        )
        ready = client.put(project_url, json=dict(expected_version=1, recipe=package["recipe"]))
        assert ready.status_code == 200, ready.text
        _, frozen = open_project(project_root(config, config.workspace_owner) / pid)
        assert frozen["assets"] == package["assets"]
        started = client.post(
            "/api/role/experiments/start", json=dict(request_id="play", blueprint_id=pid, blueprint_version=2)
        )
        assert started.status_code == 201, started.text
        run = started.json()
        sid, player, captain = run["story_id"], run["player_id"], run["actors"]["captain"]
        narrative = f"/api/role/experiments/stories/{sid}/narrative"
        stage_url = "/api/role/presentation/stage/" + player
        items_url = "/api/role/items/actors/" + player
        stage = client.get(stage_url).json()
        assert len(stage["characters"]) == 2
        assert next(c for c in stage["characters"] if c["id"] == captain)["variant"] == "neutral"
        assert [o["id"] for o in stage["objects"]] == ["rail"]  # Closed bag conceals letter.
        assert (
            client.put(
                f"/api/role/stories/{sid}/settings", json=dict(paused=False, idle_enabled=False)
            ).status_code
            == 200
        )
        for version in [1, 2]:
            advanced = client.post(
                narrative + "/advance",
                json=dict(request_id=f"opening-{version}", node_id="evening", expected_version=version),
            )
            assert advanced.status_code == 200, advanced.text
        stage = client.get(stage_url).json()
        assert next(c for c in stage["characters"] if c["id"] == captain)["variant"] == "smile"
        bag = client.get(items_url).json()["items"][0]
        opened = client.post(
            items_url + "/actions",
            json=dict(
                request_id="open-bag", item_id=bag["id"], expected_revision=bag["version"], action="open"
            ),
        )
        assert opened.status_code == 200, opened.text
        letter = next(i for i in opened.json()["items"] if i["name"] == "普通信纸")
        taken = client.post(
            items_url + "/actions",
            json=dict(
                request_id="take-letter",
                item_id=letter["id"],
                expected_revision=letter["version"],
                action="move",
                destination=dict(kind="actor", target=player),
            ),
        )
        assert taken.status_code == 200, taken.text
        letter = next(i for i in taken.json()["items"] if i["id"] == letter["id"])
        # The letter is now carried, so its authored scene position is empty.
        assert "letter-image" not in [o["id"] for o in client.get(stage_url).json()["objects"]]
        offered = client.post(
            items_url + "/offers",
            json=dict(
                request_id="offer",
                item_id=letter["id"],
                expected_revision=letter["version"],
                recipient_id=captain,
            ),
        )
        assert offered.status_code == 200, offered.text
        assert (
            next(i for i in client.get(items_url).json()["items"] if i["id"] == letter["id"])["holder_id"]
            == player
        )
        # Explicit fixture decision instead of pretending a model agreed.
        with db.session.begin() as s:
            s.get(Task, offered.json()["task_id"]).status = "completed"
        transfer = client.get(items_url).json()["transfers"][0]
        accepted = client.post(
            f"/api/role/actors/{captain}/agreements/{transfer['proposal_id']}/decisions",
            json=dict(
                request_id="fixture-accept",
                expected_version=1,
                item_ids=[transfer["item_id"]],
                decision="accept",
            ),
        )
        assert accepted.status_code == 200, accepted.text
        delivered = client.post(
            items_url + "/actions",
            json=dict(
                request_id="deliver",
                action="give",
                item_id=letter["id"],
                expected_revision=letter["version"],
                destination=dict(kind="actor", target=captain),
                agreement={k: transfer[k] for k in ["proposal_id", "version", "item_id"]},
            ),
        )
        assert delivered.status_code == 200, delivered.text
        assert next(i for i in delivered.json()["items"] if i["id"] == letter["id"])["holder_id"] == captain
        # Stop fixture follow-up jobs; the routing task below is the one under test.
        with db.session.begin() as s:
            for pending in s.scalars(select(Task).where(Task.status.in_(["queued", "running", "waiting"]))):
                pending.status = "completed"
        runtime.submit(player, config.workspace_owner, "信已经交给阿岚，现在可以继续等待。", "continue")
        task = runtime.claim("fixture-routing")
        assert task and task.session_id == player
        fake.replies["scene_routing"] = dict(
            assessments=[
                dict(
                    edge_id="continue",
                    status="met",
                    evidence=[dict(event_id=task.payload["event_id"], quote="现在可以继续等待。")],
                    reason="fixture 明确意愿",
                )
            ]
        )
        await assess(db, fake, task)
        runtime.finish(task.id, task.lease_version, {})
        frame = client.get(narrative).json()
        assert frame["auto_ready"] and frame["node_id"] == "evening"
        moved = client.post(narrative + "/auto-continue", json=frame["auto_ready"])
        assert moved.status_code == 200, moved.text
        assert moved.json()["node_id"] == "morning"
        stage = client.get(stage_url).json()
        assert next(c for c in stage["characters"] if c["id"] == captain)["outfit"] == "rain"
        assert len(fake.calls) == 1 and fake.calls[0][0] == "scene_routing"
