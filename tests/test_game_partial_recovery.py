"""A later unsafe NPC segment must not erase a published prefix or block the next turn."""

import json
from contextlib import AsyncExitStack

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention, launch

from chatbot.api import create_app
from chatbot.game_api import create_game_app
from chatbot.storage import SceneRecord, Task
from chatbot.worker import Worker


def payloads(messages):
    for message in messages:
        offset = message["content"].find("{")
        if offset >= 0:
            try:
                value = json.loads(message["content"][offset:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


@pytest.mark.parametrize("graph", [False, True])
async def test_partial_rejection_survives_recreation_and_next_player_turn(db, config, fake, graph):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake, serve_web=False)) as studio:
        _, actors, player = launch(studio)
    prefix = "修复师，你愿意说说看法吗？"
    rejected = "这里藏着一枚未经确认的蓝色钥匙。"
    next_reply = "你好，我愿意听你继续说。"

    def intent(messages):
        values = list(payloads(messages))
        if any("authorized_state" in value for value in values):
            return {"purpose": "回应问题，不预设钥匙存在。"}
        name = next(value["state"]["profile"]["name"] for value in values if "state" in value)
        return (
            intention(actors[1], expects_reply=True, wait_for_player=False)
            if name == "馆长"
            else intention(player)
        )

    fake.replies["role_intent"] = intent
    fake.replies["dialogue"] = lambda messages: (
        prefix
        if next(value["state"]["profile"]["name"] for value in payloads(messages) if "state" in value)
        == "馆长"
        else rejected
    )

    def review(messages):
        actor = next(
            value["reviewed_actor"]["id"] for value in payloads(messages) if "reviewed_actor" in value
        )
        accepted = actor == actors[0]
        return {
            "accepted": accepted,
            "checks": []
            if accepted
            else [
                {
                    "text": rejected,
                    "basis": "设定没有这枚钥匙。",
                    "supported": False,
                    "reason_code": "unsupported_fact",
                }
            ],
            "issues": [] if accepted else ["移除无依据的钥匙。"],
            "delivery": {
                "addressee_id": actors[1] if accepted else player,
                "expects_reply": accepted,
                "response_kind": "answer",
            },
        }

    fake.replies["actor_review"] = review
    worker = Worker(db, config, fake)
    async with AsyncExitStack() as stack:
        if graph:
            await worker.setup_graph(stack)
        with TestClient(create_game_app(config, db, fake, player_id=player)) as game:
            endpoint = f"/api/sessions/{player}/messages"
            response = game.post(
                endpoint,
                json={
                    "content": "馆长，请先问修复师。",
                    "client_message_id": "partial",
                    "addressees": [actors[0]],
                },
            )
            assert response.status_code == 202
            partial_id = response.json()["task_id"]
            task = worker.runtime.claim("partial-recovery")
            assert task.id == partial_id
            await worker.run_task(task)
            visible = next(t for t in game.get("/api/game/session").json()["tasks"] if t["id"] == partial_id)
            assert visible["status"] == "completed" and visible["stop_reason"] == "safety_rejected"
            snapshot = game.get(f"/api/role/players/{player}/snapshot?audio=false").json()
            assert snapshot["interaction"]["allowed"]
            assert prefix in str(snapshot["events"]) and rejected not in str(snapshot["events"])
        with db.session() as s:
            saved = s.get(Task, partial_id)
            assert saved.result["interaction_complete"] is False
            assert [r["text"] for r in saved.result["segments"]] == [prefix]
            event = s.scalar(
                select(SceneRecord).where(
                    SceneRecord.task_id == partial_id,
                    SceneRecord.actor_id == actors[0],
                    SceneRecord.kind == "speech",
                )
            )
            assert event.text == prefix and event.parent_id == saved.payload["event_id"]
        reviews = [call for call in fake.calls if call[0] == "actor_review"]
        assert len(reviews) == 4

        fake.replies["role_intent"] = intention(player)
        fake.replies["dialogue"] = next_reply
        fake.replies["actor_review"] = {
            "accepted": True,
            "issues": [],
            "delivery": {"addressee_id": player, "expects_reply": False, "response_kind": "answer"},
        }
        recreated = Worker(db, config, fake)
        if graph:
            await recreated.setup_graph(stack)
        with TestClient(create_game_app(config, db, fake, player_id=player)) as game:
            snapshot = game.get(f"/api/role/players/{player}/snapshot?audio=false").json()
            assert prefix in str(snapshot["events"]) and snapshot["interaction"]["allowed"]
            response = game.post(
                endpoint,
                json={"content": "馆长，我们继续。", "client_message_id": "next", "addressees": [actors[0]]},
            )
            assert response.status_code == 202
            next_id = response.json()["task_id"]
            assert next_id != partial_id
            next_task = recreated.runtime.claim("next-recovery")
            assert next_task.id == next_id
            await recreated.run_task(next_task)
            latest = game.get(f"/api/role/players/{player}/snapshot?audio=false").json()
            assert latest["interaction"]["allowed"] and rejected not in str(latest["events"])
        with db.session() as s:
            assert s.get(Task, next_id).status == "completed"
            assert len(list(s.scalars(select(SceneRecord).where(SceneRecord.text == prefix)))) == 1
            reply = s.scalar(
                select(SceneRecord).where(
                    SceneRecord.task_id == next_id,
                    SceneRecord.actor_id == actors[0],
                    SceneRecord.kind == "speech",
                )
            )
            assert reply.text == next_reply and reply.parent_id == s.get(Task, next_id).payload["event_id"]
