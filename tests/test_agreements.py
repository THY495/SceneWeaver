import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from support.agreements import command, consent, setup

from chatbot.agreements import ProposalInput, proposals, propose
from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.scene_state import objects, transition
from chatbot.storage import Conversation, Story


def test_requires_explicit_scoped_consent_and_replays_once(db, config, fake):
    story, actors, player, body, data = setup(db, config, fake)
    with db.session.begin() as s:
        s.scalar(select(Story).where(Story.id == story).with_for_update())
        a, b = [s.get(Conversation, aid) for aid in actors[:2]]
        before = s.get(Story, story).seq
        propose(s, a, body)
        assert s.get(Story, story).seq == before
        with pytest.raises(ValueError, match="同意"):
            transition(s, a, command(data))
        append_record(
            s,
            story_id=story,
            actor=b,
            kind="speech",
            text="好。",
            observer_ids=actors,
            dedupe="ambiguous",
            payload={"exchange": {"response_kind": "answer"}},
        )
        with pytest.raises(ValueError, match="同意"):
            transition(s, a, command(data))
        consent(s, a, data)
        decision = consent(s, b, data)
        assert consent(s, b, data).id == decision.id
        assert proposals(s, story, player) == []
        state, text, observers, _ = transition(s, a, command(data))
        result = append_record(
            s,
            story_id=story,
            actor=a,
            kind="object_state",
            text=text,
            observer_ids=observers,
            dedupe="execution",
            payload={"object": state, "agreement": {"type": "completion", **command(data)["agreement"]}},
        )
        result_id = result.id
    # A fresh session reconstructs completion; retry cannot repeat the side effect.
    with db.session.begin() as s:
        a = s.get(Conversation, actors[0])
        assert proposals(s, story, a.id)[0]["completed"][data["items"][0]["id"]] == result_id
        with pytest.raises(ValueError, match="不可执行"):
            transition(s, a, command(data))
        assert objects(s, story)[0]["version"] == 2
        assert objects(s, story)[0]["holder_id"] == actors[1]
        assert proposals(s, story, player) == []


def test_revision_refusal_withdrawal_and_id_collision(db, config, fake):
    story, actors, _, body, data = setup(db, config, fake)
    with db.session.begin() as s:
        a, b = [s.get(Conversation, aid) for aid in actors[:2]]
        consent(s, a, data)
        consent(s, b, data, "refuse", "refuse")
        with pytest.raises(ValueError, match="同意"):
            transition(s, a, command(data))
        consent(s, b, data)
        consent(s, b, data, "withdraw", "withdraw")
        with pytest.raises(ValueError, match="同意"):
            transition(s, a, command(data))
        consent(s, b, data, request="resume")
        assert transition(s, a, command(data))[0]["version"] == 2
        with pytest.raises(ValueError, match="操作标识"):
            consent(s, b, data, "refuse", "resume")
        revised = body.model_copy(
            update={"request_id": "revision", "proposal_id": data["proposal_id"], "expected_version": 1}
        )
        updated = propose(s, a, revised).payload["agreement"]
        assert updated["items"][0]["id"] == data["items"][0]["id"]
        assert updated["version"] == 2
        with pytest.raises(ValueError, match="重新确认"):
            transition(s, a, command(data))
        with pytest.raises(ValueError, match="同意"):
            transition(s, a, command(updated))
        with pytest.raises(ValueError, match="重新查看"):
            consent(s, b, data, request="stale")


def test_no_scope_expansion_or_bypass(db, config, fake):
    _, actors, _, _, data = setup(db, config, fake)
    with db.session.begin() as s:
        a, b = [s.get(Conversation, aid) for aid in actors[:2]]
        consent(s, a, data)
        consent(s, b, data)
        wrong = {**command(data), "target_id": actors[2]}
        with pytest.raises(ValueError, match="范围"):
            transition(s, a, wrong)
        with pytest.raises(ValueError, match="指定"):
            transition(s, a, {**command(data), "agreement": None})
        with pytest.raises(ValueError, match="范围"):
            consent(s, b, data, request="invalid", item_ids=["invented"])


def test_protocol_rejects_cycles_unregistered_conditions_and_nested_refs():
    base = {
        "key": "a",
        "executor_id": "actor",
        "action": {"object_id": "box", "expected_version": 1, "verb": "take"},
    }
    for items in (
        [{**base, "depends_on": ["a"]}],
        [base, base],
        [{**base, "depends_on": ["missing"]}],
        [{**base, "conditions": [{"kind": "eval", "code": "x"}]}],
        [{**base, "conditions": [{"kind": "tick"}]}],
    ):
        with pytest.raises(ValidationError):
            ProposalInput(request_id="a", source_event_id="b", items=items)


def test_redacted_consent_never_authorizes(db, config, fake):
    _, actors, _, _, data = setup(db, config, fake)
    with db.session.begin() as s:
        a, b = [s.get(Conversation, aid) for aid in actors[:2]]
        consent(s, a, data)
        event = consent(s, b, data)
        event.redacted = True
        s.flush()
        with pytest.raises(ValueError, match="不可用"):
            transition(s, a, command(data))


async def test_npc_decision_uses_real_commit_path_without_private_delivery(db, config, runtime, fake):
    from chatbot.agents import Agents
    from chatbot.storage import Message

    story, actors, player, _, data = setup(db, config, fake)
    fake.replies["role_intent"] = {
        "act": "agreement_decision",
        "source_ids": [],
        "deception": False,
        "addressee_id": None,
        "purpose": "确认我愿意交出铜盒",
        "retrieval": "context",
        "wait_for_player": True,
        "agreement_decision": {
            "proposal_id": data["proposal_id"],
            "expected_version": 1,
            "item_ids": [data["items"][0]["id"]],
            "decision": "accept",
        },
    }
    runtime.submit(player, config.workspace_owner, "馆长，你考虑好了吗？", "decision-turn")
    task = runtime.claim("agreement-test")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["answer"] == "" and result["segments"] == []
    assert runtime.finish(task.id, task.lease_version, result)
    with db.session() as s:
        view = proposals(s, story, actors[0])[0]
        assert view["decisions"][data["items"][0]["id"]][actors[0]]["decision"] == "accept"
        assert proposals(s, story, player) == []
        assert not s.scalar(select(Message).where(Message.task_id == task.id, Message.role == "assistant"))
    with TestClient(create_app(config, db, fake)) as client:
        trace = client.get(f"/api/role/stories/{story}/tasks/{task.id}/trace")
        assert trace.status_code == 200, trace.text
        audit = trace.json()
        decision = next(event for event in audit["events"] if event["agreement"])
        assert decision["agreement"]["type"] == "decision"
        assert {o["actor_id"] for o in decision["observations"]} == set(actors[:2])
        assert decision["playback"] == []
        assert audit["cost_usd"] == 0


def test_api_roundtrip_and_view_isolation(db, config, fake):
    story, actors, player, body, data = setup(db, config, fake)
    with TestClient(create_app(config, db, fake)) as client:
        response = client.post(f"/api/role/actors/{actors[0]}/agreements", json=body.model_dump())
        assert response.status_code == 200, response.text
        assert response.json()["proposal_id"] == data["proposal_id"]
        for aid in actors[:2]:
            response = client.post(
                f"/api/role/actors/{aid}/agreements/{data['proposal_id']}/decisions",
                json={
                    "request_id": "api-accept",
                    "expected_version": 1,
                    "item_ids": [data["items"][0]["id"]],
                    "decision": "accept",
                },
            )
            assert response.status_code == 200, response.text
        assert client.get(f"/api/role/stories/{story}/agreements?actor_id={player}").json() == []
        view = client.get(f"/api/role/stories/{story}/agreements?actor_id={actors[0]}").json()
        assert len(view[0]["decisions"][data["items"][0]["id"]]) == 2
        response = client.post(
            f"/api/role/actors/{player}/agreements/{data['proposal_id']}/decisions",
            json={
                "request_id": "intruder",
                "expected_version": 1,
                "item_ids": [data["items"][0]["id"]],
                "decision": "accept",
            },
        )
        assert response.status_code == 400


def test_partial_consent_and_dependency_order(db, config, fake):
    story, actors, _, body, data = setup(db, config, fake)
    with db.session.begin() as s:
        a, b = [s.get(Conversation, aid) for aid in actors[:2]]
        revised_body = body.model_dump()
        revised_body.update(request_id="chain", proposal_id=data["proposal_id"], expected_version=1)
        revised_body["items"].append(
            {
                "key": "put",
                "executor_id": b.id,
                "depends_on": ["give"],
                "action": {"object_id": "box", "expected_version": 2, "verb": "put"},
            }
        )
        data = propose(s, a, ProposalInput(**revised_body)).payload["agreement"]
        first, second = [item["id"] for item in data["items"]]
        consent(s, a, data, item_ids=[first, second])
        consent(s, b, data, item_ids=[second])
        with pytest.raises(ValueError, match="同意"):
            transition(s, a, command(data))
        put = {
            **data["items"][1]["action"],
            "agreement": {"proposal_id": data["proposal_id"], "version": 2, "item_id": second},
        }
        with pytest.raises(ValueError, match="前置"):
            transition(s, b, put)
        consent(s, b, data, request="accept-first", item_ids=[first])
        state, text, observers, _ = transition(s, a, command(data))
        append_record(
            s,
            story_id=story,
            actor=a,
            kind="object_state",
            text=text,
            observer_ids=observers,
            dedupe="first-done",
            payload={"object": state, "agreement": {"type": "completion", **command(data)["agreement"]}},
        )
        assert transition(s, b, put)[0]["holder_id"] is None


def test_failed_search_does_not_reveal_remote_holder(db, config, fake):
    from chatbot.scene_state import search_object

    story, actors, _, _, _ = setup(db, config, fake)
    with db.session.begin() as s:
        a, b = [s.get(Conversation, aid) for aid in actors[:2]]
        b.current_state = {**b.current_state, "location": "秘密储藏室"}
        append_record(
            s,
            story_id=story,
            actor=b,
            kind="object_state",
            text="铜盒已移入秘密储藏室。",
            observer_ids=[b.id],
            dedupe="hidden-move",
            payload={
                "object": {
                    "id": "box",
                    "name": "铜盒",
                    "version": 2,
                    "holder_id": b.id,
                    "location": "秘密储藏室",
                }
            },
        )
        state, text, observers, source = search_object(s, a, "box")
        assert state is None and text == "在当前可观察范围内寻找铜盒，没有找到。"
        assert observers == [a.id]
        assert source == objects(s, story, a.id)[0]["event_id"]
        assert objects(s, story, a.id)[0]["holder_id"] == a.id
        with pytest.raises(ValueError, match="已有观察"):
            search_object(s, a, "invented")
        b.current_state = {**b.current_state, "location": "main"}
        assert search_object(s, a, "box")[0]["holder_id"] == b.id
        a.current_state = {**a.current_state, "unnoticed_ids": [b.id]}
        assert search_object(s, a, "box")[0] is None
