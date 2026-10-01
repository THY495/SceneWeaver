"""Bounded retry of unfinished scene work, preserving receipts and all incurred costs."""

from datetime import timedelta

from sqlalchemy import select

from .providers import DailyBudgetExceeded, TransientProviderError
from .storage import Operation, Story, Task, now


def park_scene(db, task, error):
    if task.kind != "scene_run" or not isinstance(error, (DailyBudgetExceeded, TransientProviderError)):
        return False
    with db.session.begin() as s:
        s.scalar(select(Story).where(Story.id == task.payload["story_id"]).with_for_update())
        current = s.get(Task, task.id, with_for_update=True)
        if (
            current.status != "running"
            or current.lease_version != task.lease_version
            or not current.lease_until
            or current.lease_until <= now()
        ):
            return False
        if current.attempt >= 3:
            return False
        stamp = now()
        daily = isinstance(error, DailyBudgetExceeded)
        due = (
            stamp.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            if daily
            else stamp + timedelta(seconds=30 * 2 ** (current.attempt - 1))
        )
        current.status, current.error = "waiting", str(error)
        current.lease_until, current.lease_owner = None, None
        current.updated_at = stamp
        current.payload = {
            **current.payload,
            "recovery": {
                "reason": "daily_budget" if daily else "transport",
                "not_before": due.timestamp(),
                "after_attempt": current.attempt,
            },
        }
        return True


def recover_scene(s, story, runtime):
    from .scene_tasks import active, trigger_valid, valid_scene

    if not active(story):
        return None
    if s.scalar(
        select(Task.id)
        .where(
            Task.kind == "scene_run",
            Task.payload["story_id"].as_string() == story.id,
            Task.status.in_(["queued", "running"]),
        )
        .limit(1)
    ):
        return None
    waiting = list(
        s.scalars(
            select(Task)
            .where(
                Task.kind == "scene_run",
                Task.status == "waiting",
                Task.payload["story_id"].as_string() == story.id,
            )
            .order_by(Task.created_at)
            .with_for_update()
        )
    )
    for task in waiting:
        recovery = task.payload.get("recovery") or {}
        if recovery.get("not_before", float("inf")) > now().timestamp():
            continue
        if not valid_scene(s, task, story) or not trigger_valid(s, task):
            task.status, task.error = "cancelled", "场景或参与者已变化，未恢复旧活动。"
            continue
        if task.attempt >= 3:
            task.status = "failed"
            continue
        # Keep committed world receipts and usage. Re-plan the unfinished part against current context.
        for operation in s.scalars(select(Operation).where(Operation.task_id == task.id)):
            if "value" in operation.payload:
                s.delete(operation)
        task.status, task.error = "queued", None
        task.deadline = now() + timedelta(seconds=runtime.config.task_timeout)
        task.updated_at = now()
        task.payload = {**task.payload, "recovery": {**recovery, "resumed_at": now().timestamp()}}
        return task.id
    return None
