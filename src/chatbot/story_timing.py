"""Creator-only cross-task clock anchors; no prompts, facts or guessed client clocks."""

from sqlalchemy import select

from .storage import Conversation, Operation, SceneRecord, Task, VoicePlayback, now


def story_timing(s, story):
    captured = now()
    scoped = (
        select(Task.id)
        .outerjoin(Conversation, Conversation.id == Task.session_id)
        .where(
            Task.owner_id == story.owner_id,
            (Conversation.story_id == story.id) | (Task.payload["story_id"].as_string() == story.id),
        )
        .cte("story_task_ids", recursive=True)
    )
    scoped = scoped.union(
        select(Task.id).join(scoped, Task.parent_id == scoped.c.id).where(Task.owner_id == story.owner_id)
    )
    tasks = list(
        s.scalars(
            select(Task)
            .where(Task.id.in_(select(scoped.c.id)))
            .order_by(Task.created_at.desc(), Task.id)
            .limit(201)
        )
    )
    task_more = len(tasks) > 200
    tasks = tasks[:200]
    ids = [t.id for t in tasks]
    operations = (
        list(
            s.scalars(
                select(Operation)
                .where(Operation.task_id.in_(ids), Operation.id.startswith("timing:"))
                .order_by(Operation.id)
                .limit(1001)
            )
        )
        if ids
        else []
    )
    events = list(
        s.scalars(
            select(SceneRecord)
            .where(
                SceneRecord.story_id == story.id,
                SceneRecord.redacted.is_(False),
                SceneRecord.seq.is_not(None),
            )
            .order_by(SceneRecord.seq.desc())
            .limit(1001)
        )
    )
    event_more = len(events) > 1000
    events = events[:1000]
    playback = list(
        s.scalars(
            select(VoicePlayback)
            .join(SceneRecord, VoicePlayback.event_id == SceneRecord.id)
            .where(SceneRecord.story_id == story.id, SceneRecord.redacted.is_(False))
            .order_by(VoicePlayback.created_at.desc(), VoicePlayback.id)
            .limit(1001)
        )
    )
    return {
        "story_id": story.id,
        "origin_at": story.created_at.isoformat(),
        "captured_at": captured.isoformat(),
        "finished_at": story.settings.get("narrative", {}).get("finished_at"),
        "ending_event_id": story.settings.get("narrative", {}).get("event_id")
        if story.settings.get("narrative", {}).get("finished_at")
        else None,
        "clock_note": "阶段使用进程单调时钟，按执行启动的服务端墙钟近似关联；播放请求/回执是服务端时间，不是物理首音。",
        "truncated": {
            "tasks": task_more,
            "events": event_more,
            "traces": len(operations) > 1000,
            "playbacks": len(playback) > 1000,
        },
        "tasks": [
            dict(
                id=t.id,
                parent_id=t.parent_id,
                actor_id=t.session_id,
                kind=t.kind,
                status=t.status,
                deadline=t.deadline.isoformat(),
                overdue=t.status in {"queued", "running", "waiting"} and t.deadline < captured,
                created_at=t.created_at.isoformat(),
                updated_at=t.updated_at.isoformat(),
            )
            for t in reversed(tasks)
        ],
        "traces": [
            {
                k: trace[k]
                for k in ("id", "task_id", "attempt", "started_at", "duration_ms", "spans", "dropped_spans")
                if k in trace
            }
            for op in operations[:1000]
            if (trace := op.payload.get("timing_v1"))
        ],
        "events": [
            dict(
                id=e.id,
                seq=e.seq,
                kind=e.kind,
                actor_id=e.actor_id,
                task_id=e.task_id,
                committed_at=e.created_at.isoformat(),
            )
            for e in reversed(events)
        ],
        "playbacks": [
            dict(
                id=p.id,
                event_id=p.event_id,
                listener_id=p.session_id,
                status=p.status,
                requested_at=p.created_at.isoformat(),
                first_audio_schedule_receipt_at=p.timings.get("first_audio_schedule_receipt_at"),
                last_receipt_at=p.timings.get("last_receipt_at"),
                played_samples=p.played_samples,
                cache_hit=p.timings.get("approved_audio_cache_hit"),
            )
            for p in reversed(playback[:1000])
        ],
    }
