"""V2 item handoffs reuse the observed, versioned agreement journal."""

from pydantic import Field
from sqlalchemy import select

from ..agreements import DecisionInput, decide, proposals, propose
from ..observations import append_record, audience, perceived_cast
from ..schemas import ProposalInput
from ..storage import Conversation, StoryEvent, Task, now
from .models import Key, StrictModel
from .persistence import load
from .rules import preview_transfer


class Offer(StrictModel):
    request_id: str = Field(min_length=1, max_length=40)
    item_id: Key
    expected_revision: int = Field(ge=1)
    recipient_id: Key


def transfer_view(s, actor, world):
    from .execution import view

    visible = {item["id"]: item for item in view(s, actor, world)}
    known = {i.id for i in world.items}
    entries = []
    for proposal in proposals(s, actor.story_id, actor.id):
        for item in proposal["items"]:
            action = item["action"]
            if action["verb"] != "give" or action["object_id"] not in known:
                continue
            state = proposal["item_states"][item["id"]]["state"]
            if (
                state not in {"success", "refused", "cancelled"}
                and action["object_id"] in visible
                and visible[action["object_id"]]["version"] != action["expected_version"]
            ):
                state = "expired"
            entries.append(
                {
                    "proposal_id": proposal["proposal_id"],
                    "version": proposal["version"],
                    "item_id": item["id"],
                    "object_id": action["object_id"],
                    "expected_revision": action["expected_version"],
                    "sender_id": item["executor_id"],
                    "recipient_id": action["target_id"],
                    "description": item["description"],
                    "state": state,
                }
            )
    return {
        "transfers": entries,
        "recipients": [
            {"id": p["id"], "name": p["name"]} for p in perceived_cast(s, actor) if p["id"] != actor.id
        ],
    }


def require_interactive(s, actor, story):
    if story.settings.get("narrative", {}).get("mode") == "backstage":
        raise ValueError("幕后场景不能进行玩家交接。")
    if (
        story.settings.get("paused")
        or story.settings.get("narrative", {}).get("finished_at")
        or story.settings.get("narrative", {}).get("execution", {}).get("phase") in {"opening", "video"}
    ):
        raise ValueError("请在自由互动且剧情继续时进行交接。")
    if s.scalar(
        select(Task.id)
        .outerjoin(Conversation, Task.session_id == Conversation.id)
        .where(
            (Conversation.story_id == story.id) | (Task.payload["story_id"].as_string() == story.id),
            Task.kind.in_(["respond", "npc_step", "scene_run"]),
            Task.status.in_(["queued", "running", "waiting"]),
        )
        .limit(1)
    ):
        raise ValueError("请等待当前互动结束。")


def create_offer(s, actor, story, body, runtime):
    key = "item-offer:" + body.request_id
    request = {"actor_id": actor.id, **body.model_dump()}
    previous = s.scalar(
        select(StoryEvent).where(StoryEvent.story_id == story.id, StoryEvent.request_id == key)
    )
    if previous:
        if previous.payload["request"] != request:
            raise ValueError("同一请求不能提出不同交接。")
        return previous.payload["result"]
    require_interactive(s, actor, story)
    observers = audience(s, actor, "private", [body.recipient_id])
    _, world = load(s, story.id)
    preview_transfer(world, actor.id, body.recipient_id, body.item_id, body.expected_revision)
    item = next(i for i in world.items if i.id == body.item_id)
    source = append_record(
        s,
        story_id=story.id,
        kind="speech",
        actor=actor,
        text=f"我想递交{item.definition.name}，请你决定是否接受。",
        observer_ids=observers,
        channel="private",
        dedupe=key + ":source",
        payload={
            "handoff_offer": {
                "item_name": item.definition.name,
                "recipient_name": s.get(Conversation, body.recipient_id).current_state["profile"]["name"],
                "recipient_id": body.recipient_id,
            }
        },
    )
    proposal = propose(
        s,
        actor,
        ProposalInput.model_validate(
            {
                "request_id": key,
                "source_event_id": source.id,
                "items": [
                    {
                        "key": "handoff",
                        "executor_id": actor.id,
                        "action": {
                            "object_id": body.item_id,
                            "expected_version": body.expected_revision,
                            "verb": "give",
                            "target_id": body.recipient_id,
                        },
                    }
                ],
            }
        ),
    )
    value = proposal.payload["agreement"]
    proposal.payload = {**proposal.payload, "handoff_offer_event_id": source.id}
    consent = decide(
        s,
        actor,
        value["proposal_id"],
        DecisionInput(
            request_id=key + ":consent",
            expected_version=value["version"],
            item_ids=[value["items"][0]["id"]],
            decision="accept",
        ),
    )
    consent.payload = {**consent.payload, "handoff_offer_event_id": source.id}
    actor.revision += 1
    actor.current_state = {
        **actor.current_state,
        "last_input_at": now().timestamp(),
        "idle_issued": False,
        "presence": {**actor.current_state.get("presence", {}), "typing": False},
    }
    task = runtime.enqueue(
        s,
        "respond",
        actor.owner_id,
        {
            "text": source.text,
            "mode": "auto",
            "event_id": proposal.id,
            "handoff_proposal_id": value["proposal_id"],
            "handoff_item_id": value["items"][0]["id"],
            "audience": observers,
            "channel": "private",
            "addressees": [body.recipient_id],
        },
        session_id=actor.id,
        epoch=actor.epoch,
        priority=0,
    )
    runtime.emit(s, actor, "accepted", {"message": "正在等待接收方决定"}, task.id)
    result = {"proposal_id": value["proposal_id"], "task_id": task.id}
    s.add(StoryEvent(story_id=story.id, request_id=key, payload={"request": request, "result": result}))
    return result
