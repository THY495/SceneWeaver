"""An independent player exit proposal need not wait for unrelated NPC performance."""

from sqlalchemy import select

from ..auto_routing import assess
from ..observability.timing import annotate, timed
from ..storage import Conversation, Operation, Story


@timed("npc.player_action_preflight")
async def preflight(services, task):
    if (not getattr(services.config, "role_player_action_preflight", False) or task.kind != "respond"
            or task.payload.get("autonomous") or task.payload.get("channel", "public") != "public"
            or task.payload.get("handoff_proposal_id") or task.payload.get("item_result_event_id")):
        return None
    services.runtime.check(task.id, task.lease_version)
    with services.db.session() as s:
        player = s.get(Conversation, task.session_id)
        if (not player or not player.current_state.get("player") or s.scalar(select(Operation.id).where(
                Operation.task_id == task.id, Operation.id.startswith(f"{task.id}:committed:")).limit(1))):
            return None
    proposed = await assess(services.db, services.provider, task, player_action_only=True)
    if not proposed or not proposed["accepted"]:
        return None
    services.runtime.check(task.id, task.lease_version)
    with services.db.session() as s:
        player = s.get(Conversation, task.session_id)
        story = s.get(Story, player.story_id)
        route = story.settings.get("auto_route", {})
        if (route.get("task_id") != task.id or route.get("source_seq") != story.seq
                or route.get("node") != story.settings.get("narrative", {}).get("node")
                or route.get("scope") != "player_action"):
            return None
    annotate(player_action_preflight_hit=True)
    # The player API still checks and applies this proposal after reading.
    # No success speech, world action, or beat completion is invented here.
    return {"incremental": True, "route": "player", "answer": "", "segments": [], "actor_ids": [],
            "evidence": [], "stop_reason": "player_turn", "interaction_complete": False,
            "pending_reply_ids": [], "player_action_route_accepted": True}
