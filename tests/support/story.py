"""Shared story test helpers."""

import json


def setup_story(client):
    people = []
    story_id = None
    for name in ["馆长", "修复师", "记者"]:
        card = client.post(
            "/api/characters", json={"name": name, "world": "雾港", "core": "成年角色，尚不知账册所在。"}
        ).json()
        result = client.post(
            f"/api/characters/{card['id']}/sessions",
            json={"version": 1, "request_id": name, "story_id": story_id},
        ).json()
        people.append(result["id"])
        story_id = result["story_id"]
    return story_id, people


def event(sid, basis="confirmed", **extra):
    return {
        "request_id": "E1",
        "key": "账册位置",
        "value": "蓝账册在修复室铁盒内。",
        "occurred_at": "17:38",
        "recipients": [
            {"session_id": sid, "learned_at": "18:06", "basis": basis, "source": "主持人", **extra}
        ],
    }


def launch(client):
    story, actors = setup_story(client)
    response = client.post(f"/api/stories/{story}/player", json={"name": "访客"})
    assert response.status_code == 201
    return story, actors, response.json()["id"]


def persona(messages):
    return json.loads(messages[1]["content"].partition("：")[2])["state"]["profile"]["name"]


def intention(target, **kwargs):
    return {
        "act": "speak",
        "purpose": "回应",
        "addressee_id": target,
        "source_ids": [],
        "wait_for_player": True,
        "deception": False,
        "retrieval": "context",
        **kwargs,
    }
