import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from support.story import intention, launch

from chatbot.api import create_app
from chatbot.event_projection import requests_for
from chatbot.npc.context import NPCServices, TurnContext
from chatbot.npc.intent_reference import normalize_statement_reference
from chatbot.npc.planning import plan_actor
from chatbot.observability.timing import TimingTrace
from chatbot.observations import append_record
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation


@pytest.mark.parametrize(
    "alteration",
    [
        "missing_pending",
        "unknown_pending",
        "pending",
        "question",
        "partial",
        "missing_exchange",
        "zero_flag",
        "different_author",
        "not_cited",
        "not_speech",
        "refusal",
        "action",
    ],
)
def test_ambiguous_or_meaningful_reply_links_are_not_silently_changed(alteration):
    intent = {
        "act": "speak",
        "response_kind": "answer",
        "reply_to": "statement",
        "source_ids": ["statement"],
        "addressee_id": "author",
        "purpose": "回应对方的表态",
    }
    event = {"id": "statement", "kind": "speech", "actor_id": "author", "exchange": {"expects_reply": False}}
    info = {"pending_replies": [], "observations": [event]}
    if alteration == "missing_pending":
        info.pop("pending_replies")
    elif alteration == "unknown_pending":
        info["pending_replies"] = None
    elif alteration == "pending":
        info["pending_replies"] = [{"question_id": "other", "status": "defer"}]
    elif alteration == "question":
        event["exchange"]["expects_reply"] = True
    elif alteration == "partial":
        event["perception"] = {"complete": False}
    elif alteration == "missing_exchange":
        event.pop("exchange")
    elif alteration == "zero_flag":
        event["exchange"]["expects_reply"] = 0
    elif alteration == "different_author":
        event["actor_id"] = "somebody-else"
    elif alteration == "not_cited":
        intent["source_ids"] = []
    elif alteration == "not_speech":
        event["kind"] = "host"
    elif alteration == "refusal":
        intent["response_kind"] = "refuse"
    else:
        intent["act"] = "item_action"
    original = copy.deepcopy(intent)
    assert normalize_statement_reference(intent, info) is intent
    assert intent == original


@pytest.mark.parametrize("mode", ["ordinary", "other_pending", "many_pending", "question", "invalid_source"])
async def test_planner_repairs_only_verified_ordinary_link_without_second_call(
    db, config, runtime, fake, mode
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    with db.session.begin() as session:
        source = append_record(
            session,
            story_id=story,
            kind="speech",
            actor=session.get(Conversation, actors[1]),
            text="公告上的信息需要分清已知与未核实。" if mode != "question" else "你知道复航时间吗？",
            observer_ids=[*actors, player],
            dedupe="reference-source",
            payload={
                "exchange": {
                    "expects_reply": mode == "question",
                    "target_id": actors[0] if mode == "question" else None,
                }
            },
        ).id
        if mode in {"other_pending", "many_pending"}:
            for index in range(9 if mode == "many_pending" else 1):
                outstanding = append_record(
                    session,
                    story_id=story,
                    kind="speech",
                    actor=session.get(Conversation, actors[2]),
                    text=f"第{index}项：你何时再去查看公告？",
                    observer_ids=[*actors, player],
                    dedupe=f"other-question-{index}",
                    payload={"exchange": {"expects_reply": True, "target_id": actors[0]}},
                ).id
    runtime.submit(player, config.workspace_owner, "继续商量。", "reference-test")
    task = runtime.claim("foreground")
    memory = RoleMemoryService(db, config, runtime, fake)
    context = SimpleNamespace(assemble=memory.context)
    if mode == "many_pending":
        # The prompt is bounded to eight questions, but normalization must see
        # the authoritative list, not an empty/clipped prompt projection.
        import json

        messages, info = memory.context(task, actors[0])
        packet = json.loads(messages[1]["content"].partition("：")[2])
        assert len(packet["pending_replies"]) == 8
        assert len(info["pending_replies"]) == 9
    with db.session() as session:
        actor = session.get(Conversation, actors[0])
        before = copy.deepcopy(requests_for(session, actors[0]))
    raw = intention(
        actors[1],
        purpose="回应对方对公告的表态",
        reply_to=source,
        source_ids=[source],
        response_kind="answer",
    )
    calls = 0

    def planned(messages):
        nonlocal calls
        calls += 1
        value = copy.deepcopy(raw)
        if mode == "invalid_source" and calls == 1:
            value["source_ids"].append("unobserved-source")
        if calls == 2:
            value["reply_to"] = None
        return value

    fake.replies["role_intent"] = planned
    trace = TimingTrace(task)
    with trace.activate():
        result = await plan_actor(
            NPCServices(db, config, runtime, fake, context, None),
            task,
            TurnContext(story, player, tuple(actors)),
            actor,
            memory=SimpleNamespace(prepare_recall=AsyncMock()),
            replies=[],
            step=0,
            limit=3,
            last_actor=None,
            continuation="",
        )
    assert calls == (1 if mode in {"ordinary", "question"} else 2)
    assert result["reply_to"] == (source if mode == "question" else None)
    assert result["source_ids"] == [source]
    assert result["purpose"] == raw["purpose"]
    marks = [span["marks"] for span in trace.spans if span["name"] == "npc.intent_validation"]
    assert any("ordinary_statement_reference_normalized" in item for item in marks) == (mode == "ordinary")
    assert raw["reply_to"] == source
    with db.session() as session:
        assert requests_for(session, actors[0]) == before
        if mode in {"other_pending", "many_pending"}:
            assert next(item for item in before if item["question_id"] == outstanding)["status"] == "open"
