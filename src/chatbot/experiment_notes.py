"""Creator-only decision summaries and durable notes; never added to NPC context."""

from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .storage import Conversation, RunAnnotation, SceneRecord, Story, Task, Usage


class RunNote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(min_length=1, max_length=80)
    task_id: str | None = Field(default=None, min_length=1, max_length=36)
    note: str = Field(min_length=1, max_length=2000)


def notes_router(db, config, owner):
    router = APIRouter(prefix="/api/role/experiments/runs")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    def story(s, sid, identity, *, lock=False):
        query = select(Story).where(Story.id == sid, Story.owner_id == identity)
        row = s.scalar(query.with_for_update() if lock else query)
        if not row:
            raise HTTPException(404, "运行不存在")
        return row

    def decision(s, sid, tid, identity):
        row = s.get(Task, tid)
        if (
            not row
            or row.owner_id != identity
            or row.kind != "npc_step"
            or row.payload.get("story_id") != sid
            or row.result.get("redacted")
        ):
            raise HTTPException(404, "决策已不可用或不属于此运行")
        return row

    @router.get("/{sid}/decisions")
    def decisions(sid: str, identity=Depends(who)):
        with db.session.begin() as s:
            row = story(s, sid, identity)
            tasks = list(
                s.scalars(
                    select(Task)
                    .where(
                        Task.owner_id == identity,
                        Task.kind == "npc_step",
                        Task.payload["story_id"].as_string() == sid,
                    )
                    .order_by(Task.created_at.desc(), Task.id.desc())
                    .limit(50)
                )
            )
            if not tasks:
                return {"title": row.title, "decisions": [], "limit": 50}
            task_ids = [task.id for task in tasks]
            actor_ids = {task.payload.get("step_actor_id") for task in tasks}
            actors = {
                row.id: row
                for row in s.scalars(
                    select(Conversation).where(
                        Conversation.id.in_(actor_ids),
                        Conversation.story_id == sid,
                        Conversation.owner_id == identity,
                    )
                )
            }
            event_map = defaultdict(list)
            for event in s.scalars(
                select(SceneRecord)
                .where(
                    SceneRecord.story_id == sid,
                    SceneRecord.task_id.in_(task_ids),
                    SceneRecord.redacted.is_(False),
                )
                .order_by(SceneRecord.seq)
            ):
                event_map[event.task_id].append(event)
            usage_map = defaultdict(list)
            for call in s.scalars(
                select(Usage).where(Usage.task_id.in_(task_ids), Usage.owner_id == identity)
            ):
                usage_map[call.task_id].append(call)
            results = []
            for task in tasks:
                if task.result.get("redacted"):
                    continue
                actor = actors.get(task.payload.get("step_actor_id"))
                events = event_map[task.id]
                calls = usage_map[task.id]
                results.append(
                    {
                        "id": task.id,
                        "status": task.status,
                        "created_at": task.created_at,
                        "actor": actor.current_state.get("profile", {}).get("name", actor.title)
                        if actor
                        else "未知角色",
                        "outcome": task.result.get("experiment", {}).get("outcome"),
                        "events": [{"id": e.id, "text": e.text} for e in events],
                        "cost_usd": sum(u.cost for u in calls),
                        "reserved_usd": sum(u.reserved for u in calls),
                    }
                )
            return {"title": row.title, "decisions": results, "limit": 50}

    @router.get("/{sid}/notes")
    def notes(sid: str, identity=Depends(who)):
        with db.session.begin() as s:
            story(s, sid, identity)
            rows = s.scalars(
                select(RunAnnotation)
                .where(
                    RunAnnotation.story_id == sid,
                    RunAnnotation.owner_id == identity,
                )
                .order_by(RunAnnotation.created_at.desc(), RunAnnotation.id.desc())
                .limit(100)
            )
            result = []
            for row in rows:
                if row.task_id:
                    try:
                        decision(s, sid, row.task_id, identity)
                    except HTTPException:
                        continue
                result.append(
                    dict(
                        id=row.id,
                        task_id=row.task_id,
                        note=row.note,
                        story_seq=row.story_seq,
                        created_at=row.created_at,
                    )
                )
            return {"notes": result, "limit": 100}

    @router.post("/{sid}/notes", status_code=201)
    def add(sid: str, body: RunNote, identity=Depends(who)):
        with db.session.begin() as s:
            current = story(s, sid, identity, lock=True)
            if body.task_id:
                task = decision(s, sid, body.task_id, identity)
                if task.status in {"queued", "running", "waiting"}:
                    raise HTTPException(409, "请等待本次决策结束后记录结论")
            old = s.scalar(
                select(RunAnnotation).where(
                    RunAnnotation.owner_id == identity,
                    RunAnnotation.story_id == sid,
                    RunAnnotation.request_id == body.request_id,
                )
            )
            if old:
                if old.note != body.note or old.task_id != body.task_id:
                    raise HTTPException(409, "同一次笔记重试不能更改内容")
                return {"id": old.id}
            row = RunAnnotation(
                owner_id=identity,
                story_id=sid,
                task_id=body.task_id,
                request_id=body.request_id,
                story_seq=current.seq,
                note=body.note,
            )
            s.add(row)
            s.flush()
            return {"id": row.id}

    return router
