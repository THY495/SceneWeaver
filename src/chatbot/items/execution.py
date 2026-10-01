"""Shared player/NPC rules. Preparing never writes; committing requires the story lock."""

from ..observations import members, notices
from ..scene_state import objects
from ..storage import ItemWorldState
from .models import ItemCommand
from .perception import visible_items
from .persistence import load, observe
from .rules import RuleViolation, preview_container_state, preview_move, preview_transfer


def view(s, actor, world):
    return visible_items(world, actor.id, {p.id for p in members(s, actor.story_id) if notices(actor, p)})


def prepare(s, actor, command):
    command = ItemCommand.model_validate(command)
    if s.get(ItemWorldState, actor.story_id) is None:
        raise RuleViolation("items_not_enabled", "此故事尚未配置新版物品。")
    row, world = load(s, actor.story_id)
    known = {i["id"] for i in objects(s, actor.story_id, actor.id)} | {i["id"] for i in view(s, actor, world)}
    if command.item_id not in known or (
        command.destination
        and command.destination.kind == "container"
        and command.destination.target not in known
    ):
        raise RuleViolation("unknown_item", "目标尚未被角色发现。")
    if command.action == "give":
        from ..agreements import authorize, get_proposal
        from ..storage import Conversation

        recipient = s.get(Conversation, command.destination.target)
        if (
            not recipient
            or recipient.story_id != actor.story_id
            or not notices(actor, recipient)
            or not notices(recipient, actor)
        ):
            raise RuleViolation("recipient_unavailable", "无法与该接收者完成交接。")
        try:
            consent = command.agreement.model_dump()
            authorize(
                s,
                actor,
                {
                    "object_id": command.item_id,
                    "verb": "give",
                    "target_id": recipient.id,
                    "agreement": consent,
                },
            )
            proposal = get_proposal(s, actor, consent["proposal_id"], global_view=True)
            agreed = next(i for i in proposal["items"] if i["id"] == consent["item_id"])
            if agreed["action"]["expected_version"] != command.expected_revision:
                raise ValueError("物品已变化，需重新提出交接。")
        except ValueError as exc:
            raise RuleViolation("consent_unavailable", str(exc)) from exc
        candidate = preview_transfer(
            world, actor.id, recipient.id, command.item_id, command.expected_revision
        )
    elif command.action == "move":
        candidate = preview_move(
            world, actor.id, command.item_id, command.expected_revision, command.destination
        )
    else:
        candidate = preview_container_state(
            world, actor.id, command.item_id, command.expected_revision, opened=command.action == "open"
        )
    name = next(i.definition.name for i in world.items if i.id == command.item_id)
    label = {"move": "移动", "open": "打开", "close": "关闭", "give": "递交"}[command.action]
    text = f"{label}了{name}。"
    if command.action == "give":
        text = f"将{name}递交给{recipient.current_state['profile']['name']}。"
    return row, candidate, text


def commit(s, actor, row, candidate, key):
    row.state = candidate.model_dump()
    row.revision += 1
    observe(s, actor.story_id, candidate, key)
    for person in members(s, actor.story_id):
        person.revision += 1


def witnesses(s, actor, command):
    _, world = load(s, actor.story_id)
    return [
        person.id
        for person in members(s, actor.story_id)
        if notices(person, actor) and any(i["id"] == command["item_id"] for i in view(s, person, world))
    ]
