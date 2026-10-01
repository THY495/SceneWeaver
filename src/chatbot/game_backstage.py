"""Bounded, save-scoped automatic NPC steps for authored backstage beats."""

from sqlalchemy import select

from .observations import members
from .storage import Conversation, ExperimentInitial, GameSave, Story, StoryEvent, Task, uid

MAX_STEPS = 8
ACTIVE = ("queued", "running", "waiting")


def _progress(story):
    state = story.settings["narrative"]
    previous = story.settings.get("game_backstage_progress", {})
    return (
        previous
        if previous.get("entry_event_id") == state["event_id"]
        else {"entry_event_id": state["event_id"], "issued": 0, "status": "running"}
    )


def _transition(s, story, origin, hop):
    from .narrative import enter_node

    state = story.settings["narrative"]
    people = members(s, story.id)
    s.add(
        StoryEvent(
            id=uid(),
            story_id=story.id,
            request_id="game-directed:" + state["event_id"],
            payload={
                "source": state["node"],
                "target": hop[2],
                "edge_id": hop[1],
                "kind": "grounded_backstage_beats_completed",
                "direction_progress": state.get("direction_progress", {}),
            },
        )
    )
    enter_node(
        s,
        story,
        origin.recipe["narrative"],
        hop[2],
        origin.receipt["actors"],
        [*state["path"], hop[2]],
    )
    if story.settings["narrative"]["mode"] != "backstage":
        story.settings = {**story.settings, "paused": False}
    for actor in people:
        actor.revision += 1


def schedule_backstage(db, runtime):
    """Advance at most one save per call; no model request runs in this scheduler."""
    scope = runtime.execution_scope
    if not scope:
        return None
    with db.session() as s:
        ids = list(
            s.scalars(
                select(GameSave.story_id).where(
                    GameSave.owner_id == scope.owner_id, GameSave.release_id == scope.release_id
                )
            )
        )
    for sid in ids:
        with db.session.begin() as s:
            story = s.scalar(select(Story).where(Story.id == sid).with_for_update())
            if not story:
                continue
            state = story.settings.get("narrative", {})
            plan = story.settings.get("game_backstage_plan", {})
            if state.get("mode") != "backstage" or state.get("node") not in plan.get("directed", []):
                continue
            origin = s.get(ExperimentInitial, sid)
            if not origin:
                continue
            node = next(
                (n for n in origin.recipe["narrative"]["nodes"] if n["id"] == state["node"]),
                None,
            )
            hop = next((h for h in plan["hops"] if h[0] == state["node"]), None)
            if not node or not hop or not node.get("direction"):
                continue  # A corrupted release cannot request model work.
            progress = _progress(story)
            if progress.get("status") in {"error", "stalled"}:
                continue
            active = s.scalar(
                select(Task.id)
                .outerjoin(Conversation, Conversation.id == Task.session_id)
                .where(
                    Task.status.in_(ACTIVE),
                    Task.kind.in_(["respond", "scene_run", "npc_step"]),
                    (Conversation.story_id == sid) | (Task.payload["story_id"].as_string() == sid),
                )
                .limit(1)
            )
            if active:
                continue
            if progress.get("last_task"):
                previous = s.get(Task, progress["last_task"])
                if not previous or previous.status != "completed":
                    story.settings = {
                        **story.settings,
                        "game_backstage_progress": {**progress, "status": "error"},
                    }
                    continue
            direction = node["direction"]
            completed = state.get("direction_progress", {})
            done = (
                set(completed.get("completed", []))
                if completed.get("entry_event_id") == state["event_id"]
                else set()
            )
            remaining = [beat for beat in direction["beats"] if beat["id"] not in done]
            if not remaining:
                target = next(n for n in origin.recipe["narrative"]["nodes"] if n["id"] == hop[2])
                if target.get("mode") == "backstage":
                    people = members(s, sid)
                    target_ids = {origin.receipt["actors"][a] for a in target["participants"]}
                    arrivals = {
                        origin.receipt["actors"][p["actor"]]: p["location"]
                        for p in target.get("entry_locations", [])
                    }
                    places = {
                        arrivals.get(c.id, c.current_state.get("location", "main"))
                        for c in people
                        if c.id in target_ids
                    }
                    present = {
                        c.id
                        for c in people
                        if arrivals.get(c.id, c.current_state.get("location", "main")) in places
                    }
                    if len(places) != 1 or present != target_ids:
                        story.settings = {
                            **story.settings,
                            "game_backstage_progress": {**progress, "status": "stalled"},
                        }
                        continue
                _transition(s, story, origin, hop)
                return story.id
            if progress["issued"] >= MAX_STEPS:
                story.settings = {
                    **story.settings,
                    "game_backstage_progress": {**progress, "status": "stalled"},
                }
                continue
            people = members(s, sid)
            allowed = set(state["participants"])
            # The actual location constraint is checked again by valid_step.
            if not allowed or not allowed <= {c.id for c in people}:
                story.settings = {
                    **story.settings,
                    "game_backstage_progress": {**progress, "status": "stalled"},
                }
                continue
            aliases = origin.receipt["actors"]
            candidates = [aliases[a] for a in remaining[0]["participants"]]
            actor_id = candidates[progress["issued"] % len(candidates)]
            actor = next(c for c in people if c.id == actor_id)
            scene = actor.current_state.get("location", "main")
            present = {c.id for c in people if c.current_state.get("location", "main") == scene}
            if present != allowed:
                story.settings = {
                    **story.settings,
                    "game_backstage_progress": {**progress, "status": "stalled"},
                }
                continue
            task = runtime.enqueue(
                s,
                "npc_step",
                story.owner_id,
                {
                    "story_id": sid,
                    "scene": scene,
                    "audience": list(present),
                    "step_actor_id": actor_id,
                    "player_id": None,
                    "autonomous": True,
                    "game_backstage": True,
                    "backstage_node": state["node"],
                    "backstage_entry": state["event_id"],
                    "text": "按本场导演段落，基于自己确实知道的信息行动。",
                    "permit": {
                        "story_seq": story.seq,
                        "versions": {c.id: [c.revision, c.epoch] for c in people},
                    },
                },
                priority=45,
                dedupe=f"game-backstage:{sid}:{state['event_id']}:{progress['issued']}",
            )
            story.settings = {
                **story.settings,
                "game_backstage_progress": {
                    **progress,
                    "issued": progress["issued"] + 1,
                    "last_task": task.id,
                    "status": "running",
                },
            }
            return task.id
    return None
