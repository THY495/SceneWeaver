"""Invalidate scene work consistently for creator and player pause controls."""

from sqlalchemy import select

from .observations import members
from .storage import Task


def invalidate_scene(s, story_id, runtime):
    for actor in members(s, story_id):
        s.refresh(actor, with_for_update=True)
        actor.epoch += 1
        actor.revision += 1
        for task in s.scalars(
            select(Task).where(
                Task.session_id == actor.id,
                Task.kind == "respond",
                Task.status.in_(["running", "queued", "waiting"]),
            )
        ):
            task.status = "superseded"
        runtime.emit(s, actor, "scene_updated", {"message": "角色或场景设置已更新"})
