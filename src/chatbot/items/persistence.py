"""Private authoritative state. Never put this JSON in a player event or prompt."""

from sqlalchemy import select

from ..observations import append_record, members, notices
from ..storage import ItemWorldState, uid
from .models import ActorState, World
from .perception import visible_items


def initialize(s, story, initial, people):
    if s.get(ItemWorldState, story.id):
        raise ValueError("此运行已经初始化物品。")
    data = World.model_validate(initial).model_dump()
    ids = {i["id"]: uid() for i in data["items"]}
    actors = {alias: c.id for alias, c in people.items()}
    for actor in data["actors"]:
        actor["id"] = actors[actor["id"]]
    for item in data["items"]:
        item["id"] = ids[item["id"]]
        p = item["placement"]
        if p["kind"] == "container":
            p["target"] = ids[p["target"]]
        elif p["kind"] == "actor":
            p["target"] = actors[p["target"]]
        if item["owner_id"]:
            item["owner_id"] = actors[item["owner_id"]]
        item["source_instance_ids"] = [ids[key] for key in item["source_instance_ids"]]
    world = World.model_validate(data)
    story.settings = {**story.settings, "item_aliases": ids}
    s.add(ItemWorldState(story_id=story.id, state=world.model_dump(), revision=1))
    s.flush()
    observe(s, story.id, world, "initial")


def load(s, story_id, *, lock=True):
    query = select(ItemWorldState).where(ItemWorldState.story_id == story_id)
    row = s.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise ValueError("此故事没有新版物品配置。")
    data = row.state.copy()
    people = {p.id: p for p in members(s, story_id)}
    data["actors"] = [
        {**a, "location": people[a["id"]].current_state.get("location", "main")} for a in data["actors"]
    ]
    existing = {a["id"] for a in data["actors"]}
    data["actors"].extend(
        ActorState(id=p.id, location=p.current_state.get("location", "main")).model_dump()
        for p in people.values()
        if p.id not in existing
    )
    return row, World.model_validate(data)


def observe(s, story_id, world, request_id, actor_id=None):
    people = members(s, story_id)
    for actor in people:
        if actor_id and actor.id != actor_id:
            continue
        if not any(a.id == actor.id for a in world.actors):
            continue
        noticed = {p.id for p in people if notices(actor, p)}
        for item in visible_items(world, actor.id, noticed):
            append_record(
                s,
                story_id=story_id,
                kind="object_state",
                actor=actor,
                text=f"观察到{item['name']}。",
                observer_ids=[actor.id],
                channel="private",
                dedupe=f"item-observe:{request_id}:{actor.id}:{item['id']}",
                payload={"object": {**item, "protocol": 2}, "object_record_role": "observation"},
            )
