from fastapi.testclient import TestClient
from support.story import intention, launch

from chatbot.agents import Agents
from chatbot.api import create_app


async def test_public_input_is_not_mistaken_for_assigned_obligation(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    runtime.submit(player, config.workspace_owner, "馆长你好", "public")
    task = runtime.claim("test")
    fake.replies["role_intent"] = intention(player, reply_to=task.payload["event_id"])
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["interaction_complete"]
    assert result["segments"][0]["intent"]["reply_to"] is None


async def test_pending_npc_question_wins_over_stale_remaining_text(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    plans = iter(
        [
            intention(actors[1], expects_reply=True, wait_for_player=False, remaining_request="让对方回应"),
            intention(player),
        ]
    )
    fake.replies["role_intent"] = lambda _: next(plans)
    fake.replies["dialogue"] = "我愿意谈谈。"
    runtime.submit(
        player, config.workspace_owner, "请问修复师并让她自己回应", "handoff", addressees=[actors[0]]
    )
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("test"))
    assert result["actor_ids"] == actors[:2]


async def test_review_corrects_refusal_and_compound_request_continues(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        plans = iter([intention(player, remaining_request="说明是否愿意谈家庭"), intention(player)])
        fake.replies["role_intent"] = lambda _: next(plans)
        speeches = iter(["欢迎来访。", "我的家庭经历不便谈论。"])
        fake.replies["dialogue"] = lambda _: next(speeches)
        reviews = iter(["answer", "refuse"])
        fake.replies["actor_review"] = lambda _: {
            "accepted": True,
            "issues": [],
            "delivery": {"addressee_id": player, "expects_reply": False, "response_kind": next(reviews)},
        }
        runtime.submit(
            player, config.workspace_owner, "先欢迎我，再说说家庭", "compound", addressees=[actors[0]]
        )
        result = await Agents(db, config, runtime, fake).respond(runtime.claim("test"))
        assert result["actor_ids"] == [actors[0], actors[0]]
        assert result["interaction_complete"]
        rows = client.get(f"/api/role/stories/{story}/exchanges?actor_id={player}").json()
        assert rows[0]["status"] == "refuse"


async def test_waiting_for_player_action_does_not_continue_or_reopen_answer(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        fake.replies["role_intent"] = intention(
            player, remaining_request="玩家放下信后再拿取", awaiting_player_action=True
        )
        fake.replies["dialogue"] = "我愿意，请先把信放在桌上。"
        runtime.submit(player, config.workspace_owner, "你愿意收下信吗？", "offer", addressees=[actors[0]])
        task = runtime.claim("test")
        agents = Agents(db, config, runtime, fake)
        result = await agents.respond(task)
        assert len(result["segments"]) == 1
        assert result["stop_reason"] == "player_turn"
        assert result["interaction_complete"]
        rows = client.get(f"/api/role/stories/{story}/exchanges?actor_id={player}").json()
        assert rows[0]["status"] == "answer"
        replay = await agents.respond(task)
        assert len(replay["segments"]) == 1


def test_withdraw_reopen_and_idempotency_preserve_scope(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        runtime.submit(
            player, config.workspace_owner, "稍后再回答", "q", channel="private", addressees=[actors[0]]
        )
        rows = client.get(f"/api/role/stories/{story}/exchanges?actor_id={player}").json()
        eid = rows[0]["question_id"]
        url = f"/api/role/actors/{player}/exchanges/{eid}"
        assert client.post(url, json={"status": "cancelled", "request_id": "c"}).status_code == 200
        assert client.post(url, json={"status": "cancelled", "request_id": "c"}).status_code == 200
        assert client.post(url, json={"status": "open", "request_id": "c"}).status_code == 409
        assert client.post(url, json={"status": "open", "request_id": "r"}).status_code == 200
        assert client.get(f"/api/role/stories/{story}/exchanges?actor_id={actors[1]}").json() == []
        assert (
            client.get(f"/api/role/stories/{story}/exchanges?actor_id={player}").json()[0]["status"] == "open"
        )


async def test_later_unsafe_non_directed_segment_preserves_only_approved_reply(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    plans = iter(
        [
            intention(actors[1], expects_reply=True, wait_for_player=False),
            intention(player),
        ]
    )
    fake.replies["role_intent"] = lambda _: next(plans)
    drafts = iter(["我想先听你的。", "没有依据的候选。", "仍然没有依据的候选。", "第三稿仍没有依据。"])
    fake.replies["dialogue"] = lambda _: next(drafts)
    reviews = iter(
        [
            {"accepted": True, "issues": [], "checks": []},
            {"accepted": False, "issues": ["缺乏依据"], "checks": []},
            {"accepted": False, "issues": ["仍缺乏依据"], "checks": []},
            {"accepted": False, "issues": ["第三稿仍缺乏依据"], "checks": []},
        ]
    )
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "你们先谈", "partial-rejection", addressees=[actors[0]])
    task = runtime.claim("test")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["stop_reason"] == "safety_rejected"
    assert result["interaction_complete"] is False
    assert [segment["text"] for segment in result["segments"]] == ["我想先听你的。"]
    assert "没有依据" not in result["answer"]
