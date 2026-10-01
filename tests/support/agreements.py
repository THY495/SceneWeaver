"""Shared agreements test helpers."""

from fastapi.testclient import TestClient

from chatbot.agreements import DecisionInput, ProposalInput, decide, propose
from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.storage import Conversation
from support.story import launch


def setup(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    with db.session.begin() as s:
        a = s.get(Conversation, actors[0])
        source = append_record(
            s,
            story_id=story,
            actor=a,
            kind="speech",
            text="请帮我保管铜盒。",
            observer_ids=actors + [player],
            dedupe="request",
        )
        append_record(
            s,
            story_id=story,
            actor=a,
            kind="object_state",
            text="持有铜盒。",
            observer_ids=actors + [player],
            dedupe="box",
            payload={
                "object": {"id": "box", "name": "铜盒", "version": 1, "holder_id": a.id, "location": "main"}
            },
        )
        body = ProposalInput(
            request_id="proposal",
            source_event_id=source.id,
            items=[
                {
                    "key": "give",
                    "executor_id": a.id,
                    "action": {
                        "object_id": "box",
                        "expected_version": 1,
                        "verb": "give",
                        "target_id": actors[1],
                    },
                }
            ],
        )
        event = propose(s, a, body)
        data = event.payload["agreement"]
    return story, actors, player, body, data


def consent(s, actor, data, decision="accept", request="accept", item_ids=None):
    return decide(
        s,
        actor,
        data["proposal_id"],
        DecisionInput(
            request_id=request,
            expected_version=data["version"],
            item_ids=item_ids or [data["items"][0]["id"]],
            decision=decision,
        ),
    )


def command(data):
    return {
        **data["items"][0]["action"],
        "agreement": {
            "proposal_id": data["proposal_id"],
            "version": data["version"],
            "item_id": data["items"][0]["id"],
        },
    }
