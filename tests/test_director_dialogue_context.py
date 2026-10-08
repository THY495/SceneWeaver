import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from support.story import launch

from chatbot.api import create_app
from chatbot.npc.context import TurnContext
from chatbot.npc.director import snapshot
from chatbot.npc.director_context import bounded_public_context, common_events, event_view
from chatbot.observations import append_record
from chatbot.storage import Conversation, ExperimentInitial, Story


@pytest.mark.parametrize("delivery", ["answer", "refuse", "private", "private_shared", "partial"])
def test_snapshot_shared_delivery_ignores_private_and_partial_response(db, config, fake, delivery):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    with db.session.begin() as s:
        story = s.get(Story, sid)
        story.settings = {**story.settings, "narrative": {"node": "room", "event_id": "entry"}}
        s.add(
            ExperimentInitial(
                story_id=sid,
                owner_id=config.workspace_owner,
                request_id="links",
                request_hash="links",
                receipt={"actors": {"a": actors[0], "b": actors[1]}},
                recipe={
                    "people": [],
                    "narrative": {
                        "nodes": [
                            {
                                "id": "room",
                                "direction": {
                                    "objective": "讨论",
                                    "beats": [
                                        {
                                            "id": "beat",
                                            "title": "讨论",
                                            "objective": "交流",
                                            "participants": ["a", "b"],
                                            "completion": "说完",
                                        }
                                    ],
                                },
                            }
                        ]
                    },
                },
            )
        )
        question = append_record(
            s,
            story_id=sid,
            kind="speech",
            text="你怎么看？",
            actor=s.get(Conversation, actors[0]),
            observer_ids=actors,
            addressees=[actors[1]],
            dedupe="question",
            payload={
                "exchange": {"expects_reply": True, "target_id": actors[1]},
                "intent": {"purpose": "不应泄露的私下计划"},
                "private_notes": "隐藏动机",
            },
        )
        reply = append_record(
            s,
            story_id=sid,
            kind="speech",
            text="我不知道，留给你决定。",
            actor=s.get(Conversation, actors[1]),
            observer_ids=[actors[1]] if delivery == "private" else actors[:2],
            channel="private" if delivery in {"private", "private_shared"} else "public",
            addressees=[actors[0]],
            dedupe="reply",
            payload={
                "exchange": {
                    "reply_to": question.id,
                    "response_kind": "refuse" if delivery == "refuse" else "answer",
                }
            },
            perceptions={actors[0]: {"text": "我不知道"}} if delivery == "partial" else None,
        )
        task = SimpleNamespace(id="links-task", owner_id=config.workspace_owner, kind="respond", payload={})
        packet = json.loads(snapshot(s, task, TurnContext(sid, player, tuple(actors))).public_context)
        assert "私下计划" not in json.dumps(packet, ensure_ascii=False)
        assert "隐藏动机" not in json.dumps(packet, ensure_ascii=False)
        assert packet["exchange_status_scope"] == "shared_observed_window"
        item = next(item for item in packet["exchanges"] if item["question_id"] == question.id)
        assert item["target_id"] == actors[1]
        assert next(e for e in packet["events"] if e["event_id"] == question.id)["addressees"] == [actors[1]]
        if delivery in {"private", "partial"}:
            assert item["status"] == "open"
            assert item["response_event_id"] is None
            assert reply.id not in {e["event_id"] for e in packet["events"]}
        else:
            assert item["status"] == ("answer" if delivery == "private_shared" else delivery)
            assert item["response_event_id"] == reply.id
        if delivery == "private_shared":
            # A third participant does not acquire a private answer merely by
            # joining the director's roster, even though the question was public.
            origin = s.get(ExperimentInitial, sid)
            recipe = json.loads(json.dumps(origin.recipe))
            recipe["narrative"]["nodes"][0]["direction"]["beats"][0]["participants"].append("c")
            origin.recipe = recipe
            origin.receipt = {"actors": {"a": actors[0], "b": actors[1], "c": actors[2]}}
            wider = json.loads(snapshot(s, task, TurnContext(sid, player, tuple(actors))).public_context)
            assert wider["exchanges"][0]["status"] == "open"
            assert reply.id not in {event["event_id"] for event in wider["events"]}


def record(event_id, text, actor="a", addressees=None, payload=None):
    return SimpleNamespace(
        id=event_id,
        kind="speech",
        actor_id=actor,
        text=text,
        addressees=addressees or [],
        payload=payload or {},
    )


def test_public_packet_masks_unshared_recipient_and_partial_metadata():
    cast = [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}]
    hidden = event_view(
        record(
            "hidden",
            "你怎么看？",
            addressees=["unnoticed"],
            payload={"exchange": {"expects_reply": True, "target_id": "unnoticed"}},
        )
    )
    partial = event_view(
        record(
            "partial",
            "我听到的片段",
            actor=None,
            payload={"perception": {"complete": False}, "exchange": {"target_id": "secret"}},
        )
    )
    encoded = bounded_public_context(cast, [hidden, partial])
    assert "unnoticed" not in encoded and "secret" not in encoded
    packet = json.loads(encoded)
    assert packet["exchanges"] == []
    assert "exchange" not in packet["events"][1]
    # Even identical text cannot promote one observer's richer delivery data.
    poor = {**hidden, "addressees": [], "exchange": {}}
    assert common_events([{"hidden": hidden}, {"hidden": poor}]) == []


def test_clipped_question_cannot_leave_dangling_response_or_false_resolution():
    cast = [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}]
    question = event_view(
        record(
            "q",
            "长" * 2000,
            addressees=["b"],
            payload={"exchange": {"expects_reply": True, "target_id": "b"}},
        )
    )
    reply = event_view(
        record("r", "不知道。", actor="b", payload={"exchange": {"reply_to": "q", "response_kind": "answer"}})
    )
    packet = json.loads(bounded_public_context(cast, [question, reply], max_chars=1000))
    assert [e["event_id"] for e in packet["events"]] == ["r"]
    assert "reply_to" not in packet["events"][0]["exchange"]
    assert packet["exchanges"] == []
