"""Author permission for source-only tasks, separate from perception and facts."""

import hashlib
import json

from .directing import SceneDirection
from .storage import ExperimentInitial, Operation, Story, Task


def permits_source_tasks(direction, beat):
    return bool(beat.source_response_tasks and not (
        direction.constraints or beat.constraints or beat.role_focus
    ))


def author_source_scope(s, task, actor_id, job, ledger):
    """Return a private permission fingerprint, or None; never export author prose."""
    story = s.get(Story, ledger.get("story_id"))
    origin = s.get(ExperimentInitial, story.id) if story else None
    if not story or not origin or story.owner_id != task.owner_id:
        return None
    state = story.settings.get("narrative", {})
    node = next((n for n in (origin.recipe.get("narrative") or {}).get("nodes", [])
                 if n.get("id") == state.get("node")), None)
    if not node or not node.get("direction"):
        return None
    direction = SceneDirection.model_validate(node["direction"])
    beat = next((b for b in direction.beats if b.id == ledger["plan"]["beat_id"]), None)
    mapping = origin.receipt.get("actors", {})
    if not beat or actor_id not in {mapping.get(a) for a in beat.participants}:
        return None
    allowed = permits_source_tasks(direction, beat)
    if not allowed:
        # The separately validated direct frame grants only its exact current reply.
        frame = s.get(Operation, f"{task.id}:direct-response-frame")
        live = s.get(Task, task.id)
        response = job.grounded_response
        allowed = bool(
            frame and live and response and response.purpose == "respond"
            and live.kind == "respond" and live.payload.get("channel") == "public"
            and not live.payload.get("autonomous")
            and live.session_id == task.session_id
            and live.epoch == task.epoch and live.lease_version == task.lease_version
            and frame.payload.get("plan_revision") == ledger["revision"]
            and frame.payload.get("actor_id") == actor_id
            and frame.payload.get("source_id") == response.event_id == live.payload.get("event_id")
            and not response.job_id and job.addressees == [live.session_id]
            and mapping.get(beat.direct_response_actor) == actor_id
            and beat.participants == [beat.direct_response_actor]
            and beat.max_segments == 1 and beat.handoff == "after_beat"
            and not beat.npc_exchange_required
            and not (direction.constraints or beat.constraints or beat.role_focus)
        )
    if not allowed:
        return None
    value = {"node": node["id"], "entry": state.get("event_id"),
             "direction": direction.model_dump(), "actors": mapping}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
