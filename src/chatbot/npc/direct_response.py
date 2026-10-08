"""Compile an explicitly authored single reply, retaining all publication gates."""

import json
from contextlib import nullcontext

from ..directing import DirectorPlan, validate_plan
from ..event_projection import requests_for
from ..observability.timing import measure
from ..observations import observed_record
from ..storage import Conversation, Operation, SceneRecord, Task
from .beat_reuse import frame_key


def compile_direct_response(services, task, turn, frozen, *, previous=None, recovering=False, session=None):
    with measure("npc.direct_response_compile") as timing:
        if not getattr(services.config, "role_direct_response", False):
            timing.mark("disabled")
            return None
        if (frozen.finished or previous or recovering or task.kind != "respond" or not turn.player_id
                or task.session_id != turn.player_id or task.payload.get("autonomous")
                or task.payload.get("channel") != "public"
                or task.payload.get("handoff_proposal_id") or task.payload.get("item_result_event_id")):
            timing.mark("ineligible_turn")
            return None
        beat = next(b for b in frozen.direction.beats if b.id == frozen.beat_id)
        actor_id = beat.direct_response_actor
        if (not actor_id or beat.participants != [actor_id] or actor_id not in frozen.eligible
                or beat.max_segments != 1 or beat.handoff != "after_beat" or beat.npc_exchange_required
                or frozen.direction.constraints or beat.constraints or beat.role_focus
                or task.payload.get("addressees") != [actor_id]
                or turn.player_id not in frozen.visible_to.get(actor_id, set())):
            timing.mark("author_scope_mismatch")
            return None
        with nullcontext(session) if session is not None else services.db.session() as s:
            live = s.get(Task, task.id)
            actor = s.get(Conversation, actor_id)
            event = s.get(SceneRecord, frozen.current_player_event_id) if frozen.current_player_event_id else None
            frame = s.get(Operation, frame_key(turn.story_id))
            if (not live or live.status != "running" or live.lease_version != task.lease_version
                    or live.epoch != task.epoch or live.payload != task.payload
                    or not actor or actor.story_id != turn.story_id or actor.current_state.get("player")
                    or (frame and frame.payload.get("state") == "ready")):
                timing.mark("live_scope_changed")
                return None
            if (not event or event.redacted or event.id != task.payload.get("event_id")
                    or event.task_id != task.id or event.actor_id != turn.player_id or event.kind != "speech"
                    or event.channel != "public" or event.payload.get("acknowledgement")
                    or event.payload.get("exchange") != {"expects_reply": True, "target_id": actor_id}):
                timing.mark("input_not_explicit")
                return None
            perceived = observed_record(s, actor_id, event.id)
            shared = {row["event_id"]: row for row in json.loads(frozen.public_context)["events"]}
            if (not perceived or perceived.text != event.text or event.id not in shared
                    or shared[event.id]["text"] != event.text):
                timing.mark("input_not_fully_perceived")
                return None
            # Do not replace unfinished cast interaction or an old player obligation.
            for person in {turn.player_id, *turn.actor_ids}:
                if any(r["question_id"] != event.id and r["status"] in {"open", "partial", "defer"}
                       for r in requests_for(s, person)):
                    timing.mark("prior_question_pending")
                    return None
        timing.mark("compiled")
        plan = DirectorPlan(beat_id=beat.id, jobs=[{
            "id": "direct-response", "actor": actor_id, "addressees": [turn.player_id],
            "grounded_response": {"event_id": event.id, "delivery": "brief"},
        }], handoff=True, reason="作者允许当前明确接收者的一次真实输入回应；不代表段落已完成。")
        return validate_plan(plan, beat, frozen.eligible, frozen.visible_to)
