"""World-authority and publication checks for independent player actions."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_auto_routing import prepare

from chatbot.api import create_app
from chatbot.auto_routing import assess, resolve_player_decision
from chatbot.npc.context import TurnContext
from chatbot.npc.directed_scene import respond_directed
from chatbot.npc.performance import UnsafeDraftExhausted
from chatbot.observations import append_record
from chatbot.storage import Conversation, ExperimentInitial, Operation, SceneRecord, Story, Task, now


def decision(task, *, action="independent", quote="我决定去码头。"):
    return {
        "assessments": [
            {
                "edge_id": "leave",
                "status": "met",
                "player_action": action,
                "evidence": [{"event_id": task.payload["event_id"], "quote": quote}],
                "reason": "玩家本人当前意愿",
                "agreements": [],
                "current_agreements": [],
            }
        ]
    }


def npc_line(db, run, task, text, *, intent=None, committed=False):
    with db.session.begin() as s:
        actor = s.get(Conversation, next(iter(run["actors"].values())))
        event = append_record(
            s,
            story_id=run["story_id"],
            kind="speech",
            actor=actor,
            text=text,
            observer_ids=[actor.id, run["player_id"]],
            task_id=task.id,
            dedupe="npc:" + text,
            payload=intent or {},
        )
        event_id = event.id
        if committed:
            s.add(
                Operation(
                    id=f"{task.id}:committed:0", task_id=task.id, payload={"event_id": event.id, "intent": {}}
                )
            )
    return event_id


@pytest.mark.parametrize("action", ["dependent", "uncertain"])
def test_independence_label_cannot_be_omitted_or_replaced(action):
    task = SimpleNamespace(payload={"event_id": "p"})
    value = decision(task, action=action)
    assert not resolve_player_decision(
        value, [{"id": "leave"}], [{"id": "p", "text": "我决定去码头。"}], "p"
    )["edge_id"]


@pytest.mark.parametrize("first_fails", [True, False])
async def test_npc_failure_does_not_block_independent_player_exit(
    db, config, runtime, fake, monkeypatch, first_fails
):
    from chatbot.npc import directed_scene

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, path = prepare(client, runtime, db)
        committed_id = None
        if not first_fails:
            committed_id = npc_line(db, run, task, "雨还在下。", committed=True)
        fake.replies["scene_routing"] = decision(task)
        monkeypatch.setattr(
            directed_scene,
            "snapshot",
            lambda *a, **kw: SimpleNamespace(node_id="start", entry_event_id="entry", beat_id="b"),
        )
        monkeypatch.setattr(
            directed_scene,
            "execute_recoverable",
            AsyncMock(side_effect=UnsafeDraftExhausted("不得发表的候选")),
        )
        result = await respond_directed(
            SimpleNamespace(db=db, runtime=runtime, provider=fake),
            task,
            TurnContext(run["story_id"], run["player_id"], tuple(run["actors"].values())),
            None,
            {"plan": {"beat_id": "b"}},
        )
        assert result["stop_reason"] == "safety_rejected"
        assert result["interaction_complete"] is False
        assert result["player_action_route_accepted"] is True
        assert len(result["segments"]) == (0 if first_fails else 1)
        assert "不得发表" not in result["answer"]
        request = json.loads(fake.calls[-1][1][-1]["content"])
        assert committed_id not in {row["id"] for row in request["observations"]}
        if committed_id:
            assert committed_id in {row["id"] for row in request["constraint_observations"]}
        assert runtime.finish(task.id, task.lease_version, result=result)
        trace = client.get(f"/api/role/stories/{run['story_id']}/tasks/{task.id}/trace").json()
        assert trace["player_action_routing"]["accepted"] is True
        frame = client.get(path).json()
        assert frame["auto_ready"]
        assert client.post(path + "/auto-continue", json=frame["auto_ready"]).json()["node_id"] == "end"
        with db.session() as s:
            assert s.get(Task, task.id).result["interaction_complete"] is False
            assert s.get(Story, run["story_id"]).settings.get("auto_route", {}) == {}


async def test_current_npc_question_cannot_invent_an_independent_exit_condition(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db)
        question = npc_line(
            db,
            run,
            task,
            "你还想听天气吗？",
            intent={
                "exchange": {"target_id": run["player_id"], "expects_reply": True},
                "intent": {"requires_player_action": True},
            },
        )
        fake.replies["scene_routing"] = decision(task)
        assert (await assess(db, fake, task, player_action_only=True))["accepted"]
        messages = json.loads(fake.calls[-1][1][-1]["content"])
        assert question not in {row["id"] for row in messages["observations"]}


@pytest.mark.parametrize("mode", ["npc_positive", "made_up_quote", "dependent", "uncertain", "holding"])
async def test_rejected_or_dependent_evidence_never_grants_exit(db, config, runtime, fake, mode):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db, holding=mode == "holding")
        value = decision(task, action=mode if mode in {"dependent", "uncertain"} else "independent")
        if mode == "npc_positive":
            event = npc_line(db, run, task, "玩家已经拿起信件并去了码头。")
            value["assessments"][0]["evidence"].append({"event_id": event, "quote": "去了码头"})
        elif mode == "made_up_quote":
            value["assessments"][0]["evidence"][0]["quote"] = "我已经执行了传送。"
        fake.replies["scene_routing"] = value
        result = await assess(db, fake, task, player_action_only=True)
        assert not result or not result["accepted"]
        with db.session() as s:
            assert not s.get(Story, run["story_id"]).settings.get("auto_route")
        if mode == "holding":
            assert not fake.calls


@pytest.mark.parametrize("current_status", ["agreed", "declined", "uncertain", "old_only"])
async def test_existing_companion_agreement_requires_current_stance(
    db, config, runtime, fake, current_status
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db, companion_line="好，我跟你去码头。")
        with db.session() as s:
            old = s.scalar(
                select(SceneRecord).where(
                    SceneRecord.story_id == run["story_id"], SceneRecord.dedupe == "companion-route-intent"
                )
            )
            old_id = old.id
        new_text = "我仍然跟你去。" if current_status == "agreed" else "我改主意了，不跟你去。"
        latest = npc_line(db, run, task, new_text)
        value = decision(task)
        row = value["assessments"][0]
        old_quote = {"event_id": old_id, "quote": "我跟你去码头"}
        row["evidence"].append(old_quote)
        row["agreements"] = [
            {"actor": "curator", "status": "agreed", "evidence": [old_quote], "reason": "旧同意"}
        ]
        row["current_agreements"] = [
            {
                "actor": "curator",
                "status": "agreed" if current_status == "old_only" else current_status,
                "evidence": [old_quote]
                if current_status == "old_only"
                else [{"event_id": latest, "quote": new_text}],
                "reason": "当前立场",
            }
        ]
        fake.replies["scene_routing"] = value
        result = await assess(db, fake, task, player_action_only=True)
        assert result["accepted"] is (current_status == "agreed")


@pytest.mark.parametrize(
    "change", ["epoch", "lease", "deadline", "entry", "deleted", "text", "privacy", "recipe"]
)
async def test_player_route_cannot_commit_after_scope_change(db, config, runtime, fake, change):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db)

        def altered(messages):
            with db.session.begin() as s:
                player = s.get(Conversation, run["player_id"])
                current = s.get(Task, task.id)
                source = s.get(SceneRecord, task.payload["event_id"])
                story = s.get(Story, run["story_id"])
                if change == "epoch":
                    player.epoch += 1
                elif change == "lease":
                    current.lease_version += 1
                elif change == "deadline":
                    current.deadline = now() - timedelta(seconds=1)
                elif change == "entry":
                    story.settings = {
                        **story.settings,
                        "narrative": {**story.settings["narrative"], "event_id": "other-entry"},
                    }
                elif change == "deleted":
                    source.redacted = True
                elif change == "text":
                    source.text = "我改变主意，留在这里。"
                elif change == "privacy":
                    player.privacy_version += 1
                else:
                    origin = s.get(ExperimentInitial, run["story_id"])
                    origin.recipe = {**origin.recipe, "title": "changed-author-definition"}
            return decision(task)

        fake.replies["scene_routing"] = altered
        assert await assess(db, fake, task, player_action_only=True) is None
        with db.session() as s:
            assert not s.get(Story, run["story_id"]).settings.get("auto_route")


async def test_foreign_input_cannot_become_player_action_anchor(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db)
        foreign = npc_line(db, run, task, "我决定去码头。")
        task.payload = {**task.payload, "event_id": foreign}
        assert await assess(db, fake, task, player_action_only=True) is None
        assert not fake.calls


def test_real_model_citation_scope_mixup_repairs_only_explicit_grounded_agreement():
    """Regression for a real six-case run: old consent exists, new quote is supplemental."""
    observations = [
        {"id": "old", "kind": "speech", "actor_alias": "curator", "text": "好，我跟你去。"},
        {"id": "p", "kind": "speech", "actor_alias": "player", "text": "我决定去码头。"},
    ]
    constraints = [{"id": "new", "kind": "speech", "actor_alias": "curator", "text": "我仍然跟你去。"}]
    old_quote = {"event_id": "old", "quote": "我跟你去"}
    new_quote = {"event_id": "new", "quote": "我仍然跟你去"}
    value = decision(SimpleNamespace(payload={"event_id": "p"}))
    row = value["assessments"][0]
    row["evidence"].append(new_quote)
    row["agreements"] = [
        {"actor": "curator", "status": "agreed", "evidence": [old_quote, new_quote], "reason": "旧同意未撤回"}
    ]
    row["current_agreements"] = [{**row["agreements"][0]}]
    exits = [{"id": "leave", "requires_agreement": ["curator"]}]
    result = resolve_player_decision(value, exits, observations, "p", constraints)
    assert result["edge_id"] == "leave"
    assert {quote["event_id"] for quote in result["evidence"]} == {"p", "old"}
    assert result["evidence_scope_repair_count"] == 2
    assert row["evidence"][-1] == new_quote  # Preserve the raw candidate for audit.
    row["agreements"][0]["evidence"] = [new_quote]
    assert resolve_player_decision(value, exits, observations, "p", constraints)["edge_id"] is None
    row["agreements"][0]["evidence"] = [old_quote, {**new_quote, "quote": "伪造原话"}]
    assert resolve_player_decision(value, exits, observations, "p", constraints)["edge_id"] is None


async def test_already_superseded_player_epoch_never_starts_routing(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db)
        with db.session.begin() as s:
            s.get(Conversation, run["player_id"]).epoch += 1
        assert await assess(db, fake, task, player_action_only=True) is None
        assert not fake.calls


def test_citation_scope_repair_never_erases_another_companions_quote():
    observations = [
        {"id": "a-old", "kind": "speech", "actor_alias": "a", "text": "我跟你去。"},
        {"id": "b-old", "kind": "speech", "actor_alias": "b", "text": "我也跟你去。"},
        {"id": "p", "kind": "speech", "actor_alias": "player", "text": "我决定去码头。"},
    ]
    constraints = [{"id": "a-new", "kind": "speech", "actor_alias": "a", "text": "我仍然跟你去。"}]
    a_old = {"event_id": "a-old", "quote": "我跟你去"}
    b_old = {"event_id": "b-old", "quote": "我也跟你去"}
    a_new = {"event_id": "a-new", "quote": "我仍然跟你去"}
    value = decision(SimpleNamespace(payload={"event_id": "p"}))
    row = value["assessments"][0]
    row["evidence"].extend([a_old, b_old, a_new])
    row["agreements"] = [
        {"actor": "a", "status": "agreed", "reason": "a", "evidence": [a_old]},
        {"actor": "b", "status": "agreed", "reason": "b", "evidence": [b_old, a_new]},
    ]
    row["current_agreements"] = [
        {"actor": "a", "status": "agreed", "reason": "a", "evidence": [a_new]},
        {"actor": "b", "status": "agreed", "reason": "b", "evidence": [b_old]},
    ]
    assert (
        resolve_player_decision(
            value, [{"id": "leave", "requires_agreement": ["a", "b"]}], observations, "p", constraints
        )["edge_id"]
        is None
    )


async def test_redacted_old_agreement_during_evaluation_revokes_route(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run, task, _ = prepare(client, runtime, db, companion_line="好，我跟你去码头。")
        with db.session() as s:
            old = s.scalar(
                select(SceneRecord).where(
                    SceneRecord.story_id == run["story_id"], SceneRecord.dedupe == "companion-route-intent"
                )
            )
            old_id = old.id
        old_quote = {"event_id": old_id, "quote": "我跟你去码头"}
        value = decision(task)
        row = value["assessments"][0]
        row["evidence"].append(old_quote)
        row["agreements"] = [
            {"actor": "curator", "status": "agreed", "reason": "旧同意", "evidence": [old_quote]}
        ]
        row["current_agreements"] = row["agreements"]

        def delete_old(messages):
            with db.session.begin() as s:
                s.get(SceneRecord, old_id).redacted = True
            return value

        fake.replies["scene_routing"] = delete_old
        assert await assess(db, fake, task, player_action_only=True) is None
