"""Bounded offscreen opportunities; no player session owns the execution task."""

import hashlib
import json

from sqlalchemy import select

from .observations import members, visible_records
from .storage import Conversation, RoleMemory, SceneRecord, Story, Task, now


def opportunities(s, actor, records):
    from .agreements import proposals

    incoming = max((event.seq for event in records if event.actor_id != actor.id), default=0)
    actions = []
    for proposal in proposals(s, actor.story_id, actor.id):
        for item in proposal["items"]:
            state = proposal["item_states"][item["id"]]["state"]
            required = {proposal["initiator_id"], item["executor_id"], item["action"].get("target_id")}
            required.update(c["actor_id"] for c in item["conditions"] if c["kind"] == "consent")
            own = proposal["decisions"].get(item["id"], {}).get(actor.id)
            if actor.id in required and not own and state == "wait_reply":
                kind = "agreement_reply"
            elif item["executor_id"] == actor.id and state == "ready":
                kind = "agreement_ready"
            else:
                continue
            actions.append(
                {
                    "kind": kind,
                    "id": item["id"],
                    "version": proposal["version"],
                    "source_id": proposal["event_id"],
                    "incoming": incoming,
                }
            )
    if actions:
        return actions
    valid_ids = {event.id for event in records}
    return [
        {"kind": "goal", "id": goal.id, "version": goal.version, "incoming": incoming}
        for goal in s.scalars(
            select(RoleMemory)
            .where(
                RoleMemory.actor_id == actor.id,
                RoleMemory.kind.in_(["goal", "commitment"]),
                RoleMemory.status == "active",
                RoleMemory.source_ids.contained_by(list(valid_ids)),
            )
            .order_by(RoleMemory.created_at, RoleMemory.id)
            .limit(8)
        )
    ]


def active(story):
    from .scene_execution import opening_active, story_finished

    if opening_active(story) or story_finished(story):
        return False
    if story.settings.get("auto_route", {}).get("source_seq") == story.seq:
        return False
    if story.settings.get("narrative", {}).get("mode") == "backstage":
        return False
    return (
        not story.settings.get("paused")
        and story.settings.get("idle_enabled", True)
        and story.settings.get("scene_enabled", True)
        and story.settings.get("activity_until", 0) > now().timestamp()
    )


def trigger_valid(s, task):
    trigger = task.payload.get("trigger")
    if not trigger:
        return True  # Legacy tasks still use the original context and commit checks.
    if s.scalar(select(SceneRecord.id).where(SceneRecord.task_id == task.id).limit(1)):
        return True  # Continue a committed prefix; each subsequent action is independently checked.
    actor = s.get(Conversation, task.payload.get("trigger_actor_id"))
    if not actor:
        return False
    keys = ("kind", "id", "version", "source_id")
    return any(
        all(current.get(key) == trigger.get(key) for key in keys)
        for current in opportunities(s, actor, visible_records(s, actor.id))
    )


def valid_scene(s, task, story):
    if task.kind == "npc_step":
        from .experiments import valid_step

        return valid_step(s, task, story)
    if not active(story):
        return False
    scene = task.payload["scene"]
    people = members(s, story.id)
    if any(
        c.current_state.get("player") and c.current_state.get("location", "main") == scene for c in people
    ):
        return False
    expected = set(task.payload["audience"])
    return expected == {
        c.id
        for c in people
        if not c.current_state.get("player") and c.current_state.get("location", "main") == scene
    }


def schedule_scenes(db, runtime, owner_id):
    """Select using a character's valid goals and own incoming observations, never hidden facts."""
    with db.session() as s:
        scope = getattr(runtime, "execution_scope", None)
        ids = list(
            s.scalars(
                select(Story.id).where(
                    Story.owner_id == owner_id, Story.id.in_(scope.stories()) if scope else True
                )
            )
        )
    for sid in ids:
        with db.session.begin() as s:
            story = s.scalar(select(Story).where(Story.id == sid).with_for_update())
            if not active(story):
                continue
            from .scene_recovery import recover_scene

            resumed = recover_scene(s, story, runtime)
            if resumed:
                return resumed
            if s.scalar(
                select(Task.id)
                .where(
                    Task.kind == "scene_run",
                    Task.payload["story_id"].as_string() == sid,
                    Task.status.in_(["queued", "running", "waiting"]),
                )
                .limit(1)
            ):
                continue
            people = members(s, sid)
            occupied = {
                c.current_state.get("location", "main") for c in people if c.current_state.get("player")
            }
            for actor in people:
                scene = actor.current_state.get("location", "main")
                if actor.current_state.get("player") or scene in occupied:
                    continue
                stamp = now().timestamp()
                clocks = story.settings.get("scene_last_issued", {})
                if stamp - clocks.get(scene, 0) < story.settings.get("cooldown_seconds", 30):
                    continue
                records = visible_records(s, actor.id)
                selected = None
                for opportunity in opportunities(s, actor, records):
                    if opportunity["kind"] == "goal":
                        legacy = hashlib.sha256(
                            json.dumps(
                                [
                                    sid,
                                    scene,
                                    actor.id,
                                    opportunity["id"],
                                    opportunity["version"],
                                    opportunity["incoming"],
                                ]
                            ).encode()
                        ).hexdigest()
                        if s.scalar(select(Task.id).where(Task.dedupe_key == "scene-opportunity:" + legacy)):
                            continue
                    identity = {
                        key: value
                        for key, value in opportunity.items()
                        if key != "incoming" or opportunity["kind"] == "goal"
                    }
                    token = hashlib.sha256(
                        json.dumps([sid, scene, actor.id, identity], sort_keys=True).encode()
                    ).hexdigest()
                    dedupe = "scene-opportunity:" + token
                    if not s.scalar(select(Task.id).where(Task.dedupe_key == dedupe)):
                        selected = opportunity
                        break
                if not selected:
                    continue
                audience = [
                    c.id
                    for c in people
                    if not c.current_state.get("player") and c.current_state.get("location", "main") == scene
                ]
                task = runtime.enqueue(
                    s,
                    "scene_run",
                    owner_id,
                    {
                        "story_id": sid,
                        "scene": scene,
                        "audience": audience,
                        "autonomous": True,
                        "text": "基于自己已有的目标与观察，评估是否行动。",
                        "addressees": [actor.id],
                        "trigger_goal_id": selected["id"] if selected["kind"] == "goal" else None,
                        "trigger_actor_id": actor.id,
                        "trigger": selected,
                        "trigger_observation_seq": selected["incoming"],
                    },
                    priority=70,
                    dedupe=dedupe,
                )
                story.settings = {**story.settings, "scene_last_issued": {**clocks, scene: stamp}}
                s.flush()
                return task.id
    return None
