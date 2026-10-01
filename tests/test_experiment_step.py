import pytest
from fastapi.testclient import TestClient
from support.story import intention, launch

from chatbot.api import create_app
from chatbot.storage import Conversation, Story, Task
from chatbot.worker import Worker


def prepare(client):
    story, actors, player = launch(client)
    assert client.put(f"/api/role/stories/{story}/settings", json={"paused": True}).status_code == 200
    return story, actors, player


async def test_step_one_actor_while_paused_and_idempotent(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = prepare(client)
        path = f"/api/role/stories/{story}/steps"
        body = {"actor_id": actors[0], "request_id": "once"}
        result = client.post(path, json=body)
        assert result.status_code == 201, result.text
        tid = result.json()["task_id"]
        assert runtime.claim("not-armed", background=True) is None
        assert client.post(path, json=body).json()["task_id"] == tid
        assert client.post(path, json={**body, "actor_id": actors[1]}).status_code == 409
        assert client.post(path, json={**body, "request_id": "second"}).status_code == 409
        fake.replies["role_intent"] = intention(player, wait_for_player=False, remaining_request="继续")
        client.post(path + f"/{tid}/presence", json={"visible": True})
        task = runtime.claim("step", background=True)
        assert task.id == tid
        await Worker(db, config, fake).run_task(task)
        state = client.get(path + f"/{tid}").json()
        assert state["status"] == "completed", state
        assert state["outcome"] == "committed"
        assert len(state["events"]) == 1 and state["events"][0]["actor_id"] == actors[0]
        assert client.get(f"/api/role/stories/{story}/settings").json()["paused"]
        assert client.get(f"/api/role/stories/{story}/tasks/{tid}/trace").status_code == 200
        # Re-executing a committed task cannot issue a second decision or event.
        await Worker(db, config, fake).run_task(task)
        assert len(client.get(path + f"/{tid}").json()["events"]) == 1


async def test_item_rule_rejection_has_explicit_step_result(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = prepare(client)
        path = f"/api/role/stories/{story}/steps"
        tid = client.post(path, json={"actor_id": actors[0], "request_id": "item-reject"}).json()["task_id"]
        fake.replies["role_intent"] = intention(
            player,
            act="item_action",
            item_action={"item_id": "unknown", "expected_revision": 1, "action": "open"},
        )
        client.post(path + f"/{tid}/presence", json={"visible": True})
        await Worker(db, config, fake).run_task(runtime.claim("step", background=True))
        state = client.get(path + f"/{tid}").json()
        assert state["status"] == "completed"
        assert state["events"][0]["item_action_status"] == "rejected"
        assert state["events"][0]["item_action_code"] == "items_not_enabled"


async def test_revoke_during_generation_and_recover_committed_receipt(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = prepare(client)
        path = f"/api/role/stories/{story}/steps"
        tid = client.post(path, json={"actor_id": actors[0], "request_id": "during"}).json()["task_id"]
        client.post(path + f"/{tid}/presence", json={"visible": True})

        def cancelled_answer(messages):
            client.post(path + f"/{tid}/presence", json={"visible": False})
            return "这段晚到的结果不应提交。"

        fake.replies["dialogue"] = cancelled_answer
        await Worker(db, config, fake).run_task(runtime.claim("step", background=True))
        state = client.get(path + f"/{tid}").json()
        assert state["status"] == "cancelled" and state["events"] == []
        fake.replies.pop("dialogue")
        tid = client.post(path, json={"actor_id": actors[0], "request_id": "recover"}).json()["task_id"]
        client.post(path + f"/{tid}/presence", json={"visible": True})
        await Worker(db, config, fake).run_task(runtime.claim("step", background=True))
        with db.session.begin() as s:
            task = s.get(Task, tid)
            task.status = "queued"
            task.payload = {**task.payload, "permit": {**task.payload["permit"], "until": 0}}
        fake.replies["role_intent"] = AssertionError("Committed receipt must not plan again")
        await Worker(db, config, fake).run_task(runtime.claim("recover", background=True))
        state = client.get(path + f"/{tid}").json()
        assert state["status"] == "completed" and len(state["events"]) == 1


async def test_step_retrieval_scope_without_player_parent(db, config, runtime, fake):
    from chatbot.knowledge import Knowledge

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = prepare(client)
        tid = client.post(
            f"/api/role/stories/{story}/steps", json={"actor_id": actors[0], "request_id": "search"}
        ).json()["task_id"]
        knowledge = Knowledge(db, config, runtime, fake)
        assert await knowledge.search(config.workspace_owner, "资料", task_id=tid, actor_id=actors[0]) == []
        with pytest.raises(ValueError):
            await knowledge.search(config.workspace_owner, "资料", task_id=tid, actor_id=player)
        other, outsiders, _ = prepare(client)
        with pytest.raises(ValueError):
            await knowledge.search(config.workspace_owner, "资料", task_id=tid, actor_id=outsiders[0])


async def test_step_wait_and_revoked_permit(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = prepare(client)
        path = f"/api/role/stories/{story}/steps"
        tid = client.post(path, json={"actor_id": actors[0], "request_id": "wait"}).json()["task_id"]
        fake.replies["role_intent"] = intention(None, act="wait")
        client.post(path + f"/{tid}/presence", json={"visible": True})
        await Worker(db, config, fake).run_task(runtime.claim("step", background=True))
        state = client.get(path + f"/{tid}").json()
        assert state["outcome"] == "wait" and not state["events"]
        tid = client.post(path, json={"actor_id": actors[0], "request_id": "cancel"}).json()["task_id"]
        client.post(path + f"/{tid}/presence", json={"visible": True})
        task = runtime.claim("step", background=True)
        client.post(path + f"/{tid}/presence", json={"visible": False})
        await Worker(db, config, fake).run_task(task)
        assert client.get(path + f"/{tid}").json()["status"] == "cancelled"
        assert not client.get(path + f"/{tid}").json()["events"]


async def test_step_exhausted_unsafe_drafts_degrade_to_observable_wait(db, config, runtime, fake):
    from chatbot.observations import append_record

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = prepare(client)
        with db.session.begin() as s:
            question = append_record(
                s,
                story_id=story,
                kind="speech",
                text="你愿意继续讨论吗？",
                observer_ids=[player, actors[0]],
                dedupe="assigned-recovery",
                actor=s.get(Conversation, player),
                addressees=[actors[0]],
                payload={"exchange": {"expects_reply": True, "target_id": actors[0]}},
            )
            question_id = question.id
        path = f"/api/role/stories/{story}/steps"
        tid = client.post(path, json={"actor_id": actors[0], "request_id": "unsafe-wait"}).json()["task_id"]
        fake.replies["role_intent"] = intention(player, wait_for_player=False, reply_to=question_id)
        fake.replies["actor_review"] = {
            "accepted": False,
            "issues": ["无依据的剧情细节"],
        }
        client.post(path + f"/{tid}/presence", json={"visible": True})
        await Worker(db, config, fake).run_task(runtime.claim("step", background=True))
        state = client.get(path + f"/{tid}").json()
        assert state["status"] == "completed" and state["outcome"] == "wait"
        assert state["stop_reason"] == "safety_rejected"
        assert not state["events"]
        exchange_path = f"/api/role/stories/{story}/exchanges?actor_id={actors[0]}"

        def exchange():
            return next(row for row in client.get(exchange_path).json() if row["question_id"] == question_id)

        assert exchange()["status"] == "open"
        trace = client.get(f"/api/role/stories/{story}/tasks/{tid}/trace").json()
        spans = [span for timing in trace["timings"] for span in timing["spans"]]
        assert any(span["name"] == "npc.perform" and span["status"] == "error" for span in spans)
        # A rejected experiment must not block a later explicit attempt.
        fake.replies.pop("actor_review")
        next_tid = client.post(path, json={"actor_id": actors[0], "request_id": "after-rejection"}).json()[
            "task_id"
        ]
        client.post(path + f"/{next_tid}/presence", json={"visible": True})
        await Worker(db, config, fake).run_task(runtime.claim("recovery", background=True))
        recovered = client.get(path + f"/{next_tid}").json()
        assert recovered["outcome"] == "committed" and len(recovered["events"]) == 1
        assert exchange()["status"] == "answer"
        assert not client.get(path + f"/{tid}").json()["events"]


@pytest.mark.parametrize("change", ["expiry", "revision", "unpause"])
async def test_step_invalid_changes_prevent_commit(db, config, runtime, fake, change):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = prepare(client)
        path = f"/api/role/stories/{story}/steps"
        tid = client.post(path, json={"actor_id": actors[0], "request_id": change}).json()["task_id"]
        client.post(path + f"/{tid}/presence", json={"visible": True})
        task = runtime.claim("step", background=True)
        with db.session.begin() as s:
            if change == "expiry":
                t = s.get(Task, tid)
                t.payload = {**t.payload, "permit": {**t.payload["permit"], "until": 0}}
            elif change == "revision":
                s.get(Conversation, actors[1]).revision += 1
            else:
                st = s.get(Story, story)
                st.settings = {**st.settings, "paused": False}
        await Worker(db, config, fake).run_task(task)
        state = client.get(path + f"/{tid}").json()
        assert state["status"] == "failed" and not state["events"]


def test_step_cross_story_and_budget(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = prepare(client)
        other, other_actors, _ = prepare(client)
        path = f"/api/role/stories/{story}/steps"
        assert client.post(path, json={"actor_id": other_actors[0], "request_id": "bad"}).status_code == 404
        tid = client.post(path, json={"actor_id": actors[0], "request_id": "good"}).json()["task_id"]
        assert client.get(f"/api/role/stories/{other}/steps/{tid}").status_code == 404
        assert (
            client.post(
                f"/api/role/stories/{other}/steps/{tid}/presence", json={"visible": False}
            ).status_code
            == 404
        )
        config.daily_budget_usd = 0
        assert (
            client.post(
                f"/api/role/stories/{other}/steps", json={"actor_id": other_actors[0], "request_id": "budget"}
            ).status_code
            == 409
        )
