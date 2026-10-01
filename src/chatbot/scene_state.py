"""Small event-sourced world model and visible conversation obligations."""

from sqlalchemy import select

from .observations import append_record, audience, members, notices
from .storage import Observation, SceneRecord


def is_object_observation(event):
    """Read old archives without rewriting them or promoting a sighting to a mutation."""
    role = event.payload.get("object_record_role")
    if role is not None:
        return role == "observation"
    return (event.dedupe or "").startswith("observe-object:") or (event.payload.get("intent") or {}).get(
        "act"
    ) == "search"


def objects(s, story_id, viewer_id=None):
    query = select(SceneRecord).where(SceneRecord.story_id == story_id, SceneRecord.kind == "object_state")
    if viewer_id:
        query = query.join(Observation).where(Observation.actor_id == viewer_id)
    latest = {}
    for event in s.scalars(query.order_by(SceneRecord.seq)):
        if not viewer_id and is_object_observation(event):
            continue
        item = event.payload.get("object")
        if item:
            latest[item["id"]] = None if event.redacted else {**item, "event_id": event.id, "seq": event.seq}
    return [item for item in latest.values() if item]


class WorldActionError(ValueError):
    """An attempted physical action cannot currently succeed; not a provider outage."""


def transition(s, actor, command):
    """Validate a concrete action; callers must lock the story before committing it."""
    from .agreements import authorize

    authorize(s, actor, command)
    item = next((x for x in objects(s, actor.story_id) if x["id"] == command["object_id"]), None)
    known = {x["id"] for x in objects(s, actor.story_id, actor.id)}
    if any(
        x["id"] == command["object_id"] and x.get("protocol") == 2
        for x in objects(s, actor.story_id, actor.id)
    ):
        raise WorldActionError("此物品使用新版规则，请通过新版物品操作入口执行。")
    if not item or item["id"] not in known or item["version"] != command["expected_version"]:
        raise WorldActionError("物品不可操作或状态已变化，请重新观察。")
    scene = actor.current_state.get("location", "main")
    people = {c.id: c for c in members(s, actor.story_id)}
    verb, target = command["verb"], command.get("target_id")
    if verb == "take":
        if item["holder_id"] or item["location"] != scene:
            raise WorldActionError("只能拿起当前场景中无人持有的物品。")
        holder = actor.id
        text = f"拿起了{item['name']}。"
    elif verb in {"put", "give"}:
        if item["holder_id"] != actor.id:
            raise WorldActionError("只能操作自己持有的物品。")
        holder = None
        text = f"将{item['name']}放在当前场景。"
        if verb == "give":
            recipient = people.get(target)
            if (
                not recipient
                or not notices(actor, recipient)
                or target == actor.id
                or recipient.current_state.get("location", "main") != scene
            ):
                raise WorldActionError("交接对象必须是同场的另一个角色。")
            if recipient.current_state.get("player"):
                raise ValueError("不能替玩家接受物品；可以放下物品，等待玩家自行拿取。")
            holder = target
            text = f"将{item['name']}交给了{recipient.current_state['profile']['name']}。"
    else:
        raise ValueError("不支持该行动。")
    state = {k: item[k] for k in ("id", "name", "version", "holder_id", "location")}
    state.update(holder_id=holder, location=scene, version=item["version"] + 1)
    observers = [
        sid
        for sid in audience(s, actor)
        if notices(people[sid], actor) and (verb != "give" or notices(people[sid], people[target]))
    ]
    return state, text, observers, item["event_id"]


def search_object(s, actor, object_id):
    """A local attempt can fail without receiving the object's hidden actual location."""
    known = next((obj for obj in objects(s, actor.story_id, actor.id) if obj["id"] == object_id), None)
    if not known:
        raise ValueError("查找对象必须来自角色已有观察")
    current = next((obj for obj in objects(s, actor.story_id) if obj["id"] == object_id), None)
    people = {c.id: c for c in members(s, actor.story_id)}
    holder = people.get(current["holder_id"]) if current else None
    location = holder.current_state.get("location", "main") if holder else (current or {}).get("location")
    found = bool(
        current
        and location == actor.current_state.get("location", "main")
        and (not holder or notices(actor, holder))
    )
    if not found:
        return None, f"在当前可观察范围内寻找{known['name']}，没有找到。", [actor.id], known["event_id"]
    state = {key: current[key] for key in ("id", "name", "version", "holder_id", "location")}
    state["location"] = location
    text = f"在当前场景找到了{known['name']}"
    text += f"，由{holder.current_state['profile']['name']}持有。" if holder else "。"
    return state, text, [actor.id], known["event_id"]


def exchanges(records):
    """Only committed, visible replies resolve a question. Failures never count as silence."""
    pending = {}
    for event in records:
        if event.redacted:
            continue
        value = event.payload.get("exchange", {})
        update = event.payload.get("exchange_update", {})
        previous = pending.get(update.get("question_id"))
        if previous and previous["asker_id"] == event.actor_id:
            previous.update(status=update["status"], response_event_id=event.id)
        reply = pending.get(value.get("reply_to"))
        if reply and reply["status"] in {"open", "partial", "defer"} and event.actor_id == reply["target_id"]:
            reply.update(status=value.get("response_kind", "answer"), response_event_id=event.id)
        if value.get("expects_reply") and value.get("target_id"):
            pending[event.id] = {
                "question_id": event.id,
                "asker_id": event.actor_id,
                "target_id": value["target_id"],
                "question": event.text,
                "status": "open",
                "response_event_id": None,
                "task_id": event.task_id,
            }
    return list(pending.values())


def observe_scene(s, actor, request_id):
    dedupe = f"observe-scene:{actor.id}:{request_id}"
    old = s.scalar(
        select(SceneRecord).where(SceneRecord.story_id == actor.story_id, SceneRecord.dedupe == dedupe)
    )
    if old:
        if old.redacted:
            raise ValueError("该观察记录已删除。")
        return False
    people = {c.id: c for c in members(s, actor.story_id)}
    scene = actor.current_state.get("location", "main")
    for item in objects(s, actor.story_id):
        holder = people.get(item["holder_id"])
        location = holder.current_state.get("location", "main") if holder else item["location"]
        if location != scene:
            continue
        if holder and not notices(actor, holder):
            continue
        state = {k: item[k] for k in ("id", "name", "holder_id", "location", "version")}
        state["location"] = location
        # A contemporary observation does not give access to the earlier transfer's private context.
        append_record(
            s,
            story_id=actor.story_id,
            kind="object_state",
            actor=actor,
            text=f"观察到{item['name']}，"
            + (f"由{holder.current_state['profile']['name']}持有。" if holder else f"位于{scene}。"),
            observer_ids=[actor.id],
            channel="private",
            dedupe=f"observe-object:{actor.id}:{request_id}:{item['id']}",
            payload={
                "object": state,
                "object_record_role": "observation",
                "context_source_ids": [item["event_id"]],
            },
        )
    append_record(
        s,
        story_id=actor.story_id,
        kind="observation",
        actor=actor,
        text="观察了当前场景的已登记物品。",
        observer_ids=[actor.id],
        channel="private",
        dedupe=dedupe,
    )
    return True
