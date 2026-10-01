from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Limit, Placement, World
from chatbot.narrative_demo import ferry_story
from chatbot.storage import Conversation, ItemWorldState, SceneRecord, Task


def test_lost_handoff_response_recovers_without_duplicate_effects(db, config, fake):
    """Discard a fully committed ASGI response, then inspect and replay the same request."""
    config.product = "roleplay"
    app = create_app(config, db, fake)
    fault = {"armed": False}

    async def lose_response(scope, receive, send):
        if scope["type"] == "http" and fault["armed"] and scope["path"].endswith("/actions"):
            fault["armed"] = False
            messages = []

            async def capture(message):
                messages.append(message)

            await app(scope, receive, capture)
            assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 200
            raise ConnectionError("Test transport discarded the committed response")
        await app(scope, receive, send)

    with TestClient(lose_response) as client:
        run, path, offer = setup(client)
        body = decide(client, db, run, path, offer, "accept")
        with db.session() as s:
            before = len(s.scalars(select(Task).where(Task.session_id == run["player_id"])).all())
        fault["armed"] = True
        with pytest.raises(ConnectionError, match="discarded"):
            client.post(path + "/actions", json=body)
        snapshot = client.get(f"/api/role/players/{run['player_id']}/snapshot").json()
        assert snapshot["inventory"]["items"][0]["holder_id"] == run["actors"]["captain"]
        assert snapshot["inventory"]["transfers"][0]["state"] == "success"
        recovered = client.post(path + "/actions", json=body)
        assert recovered.status_code == 200, recovered.text
        assert client.post(path + "/actions", json=body).json() == recovered.json()
        with db.session() as s:
            tasks = s.scalars(select(Task).where(Task.session_id == run["player_id"])).all()
            assert len(tasks) == before + 1
            assert sum(task.id == recovered.json()["task_id"] for task in tasks) == 1
            events = s.scalars(
                select(SceneRecord).where(
                    SceneRecord.story_id == run["story_id"],
                    SceneRecord.dedupe == "item-action:" + body["request_id"],
                )
            ).all()
            assert len(events) == 1
        changed = {**body, "expected_revision": 2}
        assert client.post(path + "/actions", json=changed).status_code == 409


def setup(client):
    value = ferry_story()
    value.pop("narrative")
    value["people"] = value["people"][:1]
    value["item_world"] = World(
        items=[instantiate("paper-sheet", "letter", Placement(kind="actor", target="player"))],
        actors=[
            ActorState(
                id=p["alias"],
                location="候船亭",
                lift_limit_g=Limit(mode="unlimited"),
                carry_limit_g=Limit(mode="unlimited"),
            )
            for p in [value["player"], *value["people"]]
        ],
    ).model_dump()
    bid = str(uuid4())
    assert (
        client.put(
            f"/api/role/experiments/blueprints/{bid}", json={"expected_version": 0, "recipe": value}
        ).status_code
        == 200
    )
    run = client.post(
        "/api/role/experiments/start", json={"request_id": bid, "blueprint_id": bid, "blueprint_version": 1}
    ).json()
    client.put(f"/api/role/stories/{run['story_id']}/settings", json={"paused": False, "idle_enabled": False})
    pid, npc = run["player_id"], run["actors"]["captain"]
    path = f"/api/role/items/actors/{pid}"
    item = client.get(path).json()["items"][0]
    body = {"request_id": str(uuid4()), "item_id": item["id"], "expected_revision": 1, "recipient_id": npc}
    result = client.post(path + "/offers", json=body)
    assert result.status_code == 200, result.text
    assert client.post(path + "/offers", json=body).json() == result.json()
    assert client.get(path).json()["items"][0]["holder_id"] == pid
    assert client.get(path).json()["transfers"][0]["state"] == "wait_reply"
    return run, path, result.json()


def decide(client, db, run, path, offer, decision):
    with db.session.begin() as s:
        s.get(Task, offer["task_id"]).status = "completed"
    transfer = client.get(path).json()["transfers"][0]
    npc = run["actors"]["captain"]
    result = client.post(
        f"/api/role/actors/{npc}/agreements/{transfer['proposal_id']}/decisions",
        json={
            "request_id": str(uuid4()),
            "expected_version": 1,
            "item_ids": [transfer["item_id"]],
            "decision": decision,
        },
    )
    assert result.status_code == 200, result.text
    return {
        "request_id": str(uuid4()),
        "action": "give",
        "item_id": transfer["object_id"],
        "expected_revision": 1,
        "destination": {"kind": "actor", "target": npc},
        "agreement": {k: transfer[k] for k in ["proposal_id", "version", "item_id"]},
    }


@pytest.mark.parametrize("change", ["none", "refuse", "withdraw", "moved", "away", "capacity", "scene_busy"])
def test_transfer_requires_consent_and_rechecks_physics(db, config, fake, change):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, path, offer = setup(client)
        body = decide(client, db, run, path, offer, "refuse" if change == "refuse" else "accept")
        if change == "scene_busy":
            with db.session.begin() as s:
                s.add(
                    Task(
                        id=str(uuid4()),
                        kind="scene_run",
                        owner_id=config.workspace_owner,
                        payload={"story_id": run["story_id"]},
                        status="queued",
                        deadline=s.get(Task, offer["task_id"]).deadline,
                    )
                )
        if change == "withdraw":
            ref = body["agreement"]
            client.post(
                f"/api/role/actors/{run['player_id']}/agreements/{ref['proposal_id']}/decisions",
                json={
                    "request_id": "withdraw",
                    "expected_version": 1,
                    "item_ids": [ref["item_id"]],
                    "decision": "withdraw",
                },
            )
        if change in {"moved", "away", "capacity"}:
            with db.session.begin() as s:
                if change == "away":
                    c = s.get(Conversation, run["actors"]["captain"])
                    c.current_state = {**c.current_state, "location": "码头外"}
                else:
                    row = s.get(ItemWorldState, run["story_id"])
                    world = World.model_validate(row.state)
                    if change == "moved":
                        world.items[0].revision += 1
                    else:
                        next(
                            a for a in world.actors if a.id == run["actors"]["captain"]
                        ).carry_limit_g = Limit(mode="limited", value=0)
                    row.state = world.model_dump()
        result = client.post(path + "/actions", json=body)
        if change == "none":
            assert result.status_code == 200, result.text
            assert client.post(path + "/actions", json=body).json() == result.json()
            assert client.get(path).json()["items"][0]["holder_id"] == run["actors"]["captain"]
            assert client.get(path).json()["transfers"][0]["state"] == "success"
            with db.session() as s:
                event = s.scalar(
                    select(SceneRecord).where(
                        SceneRecord.story_id == run["story_id"],
                        SceneRecord.dedupe == "item-action:" + body["request_id"],
                    )
                )
                assert event.payload["item_action_result"]["status"] == "completed"
                from chatbot.observations import observed_record, record_data

                observed = record_data(
                    observed_record(s, run["actors"]["captain"], event.id), viewer_id=run["actors"]["captain"]
                )
                assert observed["detail"] == {"item_action_result": {"status": "completed"}}
                assert observed["source_class"] == "item_action_result"
            with db.session.begin() as s:
                s.get(Task, result.json()["task_id"]).status = "completed"
            assert client.post(path + "/actions", json={**body, "request_id": "duplicate"}).status_code == 409
        else:
            assert result.status_code == 409, result.text
            assert client.get(path).json()["items"][0]["holder_id"] == run["player_id"]


async def test_npc_decision_uses_existing_agreement_journal(db, config, runtime, fake):
    from support.story import intention

    from chatbot.worker import Worker

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, path, offer = setup(client)
        transfer = client.get(path).json()["transfers"][0]
        fake.replies["role_intent"] = intention(
            run["player_id"],
            act="agreement_decision",
            agreement_decision={
                "proposal_id": transfer["proposal_id"],
                "expected_version": 1,
                "item_ids": [transfer["item_id"]],
                "decision": "accept",
            },
        )
        task = runtime.claim("gift-test")
        assert task.id == offer["task_id"]
        await Worker(db, config, fake).run_task(task)
        assert client.get(path).json()["transfers"][0]["state"] == "ready"
        assert client.get(path).json()["items"][0]["holder_id"] == run["player_id"]
        received = client.post(
            path + "/actions",
            json={
                "request_id": "complete",
                "action": "give",
                "item_id": transfer["object_id"],
                "expected_revision": 1,
                "destination": {"kind": "actor", "target": run["actors"]["captain"]},
                "agreement": {k: transfer[k] for k in ["proposal_id", "version", "item_id"]},
            },
        )
        assert received.status_code == 200, received.text
        fake.replies["role_intent"] = intention(run["player_id"])
        reaction = runtime.claim("gift-test")
        assert reaction.id == received.json()["task_id"]
        await Worker(db, config, fake).run_task(reaction)
        with db.session() as s:
            saved = s.get(Task, reaction.id)
            assert saved.status == "completed", saved.error
            assert (
                saved.result["segments"][0]["intent"]["result_event_id"]
                == reaction.payload["item_result_event_id"]
            )


@pytest.mark.parametrize("decision", ["accept", "refuse"])
async def test_targeted_handoff_repairs_plain_speech_and_skips_scene_director(
    db, config, runtime, fake, monkeypatch, decision
):
    """A real ferry run once spoke about the notice and left the offer pending."""
    from unittest.mock import AsyncMock

    from support.story import intention

    from chatbot.npc import directed_scene, director
    from chatbot.worker import Worker

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, path, offer = setup(client)
        transfer = client.get(path).json()["transfers"][0]
        monkeypatch.setattr(director, "snapshot", lambda *args, **kwargs: object())
        directed = AsyncMock()
        monkeypatch.setattr(directed_scene, "respond_directed", directed)
        calls = 0

        def choose(messages):
            nonlocal calls
            calls += 1
            if calls == 1:
                return intention(run["player_id"], act="speak", purpose="继续讨论公告")
            return intention(
                run["player_id"],
                act="agreement_decision",
                agreement_decision={
                    "proposal_id": transfer["proposal_id"],
                    "expected_version": transfer["version"],
                    "item_ids": [transfer["item_id"]],
                    "decision": decision,
                },
            )

        fake.replies["role_intent"] = choose
        task = runtime.claim("targeted-handoff")
        assert task.id == offer["task_id"]
        await Worker(db, config, fake).run_task(task)
        directed.assert_not_called()
        assert calls == 2
        assert client.get(path).json()["transfers"][0]["state"] == (
            "ready" if decision == "accept" else "refused"
        )
        assert client.get(path).json()["items"][0]["holder_id"] == run["player_id"]
        with db.session() as s:
            saved = s.get(Task, task.id)
            assert saved.status == "completed", saved.error
            assert saved.result["segments"][0]["intent"]["act"] == "agreement_decision"
            assert saved.result["segments"][0]["intent"]["agreement_decision"]["decision"] == decision


@pytest.mark.parametrize("decision", ["accept", "refuse", "withdraw"])
def test_stage_handoff_keeps_journal_and_respects_observation(db, config, fake, decision):
    from chatbot.storage import Observation

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, path, offer = setup(client)
        decide(client, db, run, path, offer, decision)
        url = f"/api/role/stories/{run['story_id']}/timeline?actor_id={run['player_id']}"
        original = client.get(url).json()["events"]
        rendered = client.get(url + "&stage=true").json()["events"]
        assert original == [{k: v for k, v in e.items() if k != "stage"} for e in rendered]
        offer_event = next(e for e in rendered if e.get("stage", {}).get("mode") == "narration")
        redundant = [e for e in rendered if e.get("stage", {}).get("mode") == "redundant"]
        assert len(redundant) == 2
        assert all(e["stage"]["with_event_id"] == offer_event["id"] for e in redundant)
        response = next(e for e in rendered if e["actor_id"] == run["actors"]["captain"])
        assert response["stage"]["mode"] == "narration"
        assert {"accept": "同意", "refuse": "拒绝", "withdraw": "撤回"}[decision] in response["stage"]["text"]
        assert ("尚未完成交接" in response["stage"]["text"]) == (decision == "accept")
        assert client.get(path).json()["items"][0]["holder_id"] == run["player_id"]

        # Do not summarize a partly heard decision using its complete hidden metadata.
        with db.session.begin() as s:
            observation = s.get(Observation, (run["player_id"], response["id"]))
            observation.perception = {"text": "听不清。", "speaker_known": False}
        partial = next(
            e for e in client.get(url + "&stage=true").json()["events"] if e["id"] == response["id"]
        )
        assert "stage" not in partial
        assert partial["text"] == "听不清。"

        # A missing/redacted source cannot be recovered through a presentation shortcut.
        with db.session.begin() as s:
            s.get(Observation, (run["player_id"], response["id"])).perception = {}
            s.get(SceneRecord, offer_event["id"]).redacted = True
        scoped = client.get(url + "&stage=true").json()["events"]
        assert all("stage" not in e for e in scoped)
