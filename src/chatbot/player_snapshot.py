"""A read-only player projection. Caller opens one REPEATABLE READ transaction."""

from sqlalchemy import or_, select

from .items.execution import view
from .items.persistence import load
from .items.stage import handoff_stage
from .items.transfers import transfer_view
from .narrative import player_view
from .observations import observed_record, perceive
from .presentation import stage_view
from .storage import ExperimentInitial, ItemWorldState, Observation, SceneRecord, Task


def player_snapshot(s, player, story, settings, anchor=None, *, audio=True):
    origin = s.get(ExperimentInitial, story.id)
    frame = player_view(story, origin.recipe if origin else {})
    query = (
        select(SceneRecord, Observation)
        .join(Observation)
        .where(
            SceneRecord.story_id == story.id,
            SceneRecord.redacted.is_(False),
            Observation.actor_id == player.id,
        )
    )
    anchor = anchor or frame.get("entry_event_id")
    known = observed_record(s, player.id, anchor) if anchor else None
    if known and known.story_id == story.id:
        recent_floor = s.scalar(
            query.with_only_columns(SceneRecord.seq).order_by(SceneRecord.seq.desc()).offset(99).limit(1)
        )
        query = query.where(SceneRecord.seq >= min(known.seq, recent_floor or 0))
    if known:
        rows = list(s.execute(query.order_by(SceneRecord.seq).limit(2101)))
        more_after = len(rows) > 2100
        rows = rows[:2100]
    else:
        rows = list(reversed(s.execute(query.order_by(SceneRecord.seq.desc()).limit(100)).all()))
        more_after = False
    events = []
    from .player_release import project

    for event, observation in rows:
        scoped = perceive(event, observation)
        projected = project(s, player, scoped, audio=audio)
        events.append(
            projected
            if projected.get("presentation_pending")
            else {**projected, **handoff_stage(s, scoped, player.id)}
        )
    inventory = {"enabled": False, "items": [], "transfers": [], "recipients": []}
    if s.get(ItemWorldState, story.id):
        _, world = load(s, story.id, lock=False)
        inventory = {"enabled": True, "items": view(s, player, world), **transfer_view(s, player, world)}
    # Offscreen scene_run work belongs to the same story but must not freeze the
    # player's controls. Only work addressing this player can block their turn.
    task_active = (
        s.scalar(
            select(Task.id)
            .where(
                or_(
                    (Task.kind == "respond") & (Task.session_id == player.id),
                    (Task.kind == "npc_step")
                    & (Task.payload["story_id"].as_string() == story.id)
                    & (Task.payload["player_id"].as_string() == player.id),
                ),
                Task.status.in_(["queued", "running", "waiting"]),
            )
            .limit(1)
        )
        is not None
    )
    reason = (
        "故事已结束。"
        if frame.get("finished_at")
        else "当前不在玩家场景。"
        if frame.get("mode") == "backstage"
        else "继续游玩后才能互动。"
        if settings["paused"]
        else "请先读完开场剧情。"
        if frame.get("execution", {}).get("phase") in {"opening", "video"}
        else "请等待当前回应完成。"
        if task_active
        else ""
    )
    stage = stage_view(s, player)
    return {
        "story_id": story.id,
        "actor_id": player.id,
        "epoch": player.epoch,
        "frame": frame,
        "settings": settings,
        "events": events,
        "characters": stage["characters"],
        "objects": stage["objects"],
        "inventory": inventory,
        "interaction": {"allowed": not reason, "reason": reason, "responding": task_active},
        "history_truncated": False,
        "history_more_after": more_after,
        "history_next_anchor": rows[-1][0].id if more_after and rows else None,
    }
