"""Creator-only run records, exported from a consistent database read without execution."""

from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from .observations import perceive, record_data
from .storage import (
    Conversation,
    EventAnnotation,
    ExperimentInitial,
    Message,
    Observation,
    Operation,
    RoleMemory,
    RoleSnapshot,
    RunAnnotation,
    SceneRecord,
    Story,
    Task,
    Usage,
    now,
)


def bounded(s, query, limit, label):
    rows = list(s.scalars(query.limit(limit + 1)))
    if len(rows) > limit:
        raise HTTPException(413, f"{label}超过本版导出上限{limit}，未生成截断档案。")
    return rows


def event_view(record, actor_id=None):
    value = record_data(record, viewer_id=actor_id)
    result = {
        key: value.get(key)
        for key in (
            "id",
            "seq",
            "kind",
            "source_class",
            "actor_id",
            "speaker",
            "text",
            "channel",
            "scene",
            "occurred_at",
            "recorded_at",
            "parent_id",
            "perception",
        )
    }
    result["detail"] = {
        key: value["detail"][key]
        for key in ("key", "value", "basis", "source", "learned_at", "replaces_event_id")
        if key in value["detail"]
    }
    return result


def archive(s, sid, who):
    story = s.scalar(select(Story).where(Story.id == sid, Story.owner_id == who))
    if not story:
        raise HTTPException(404, "实验剧情不存在")
    people = bounded(
        s,
        select(Conversation)
        .where(Conversation.story_id == sid, Conversation.owner_id == who)
        .order_by(Conversation.id),
        30,
        "角色数",
    )
    actor_ids = {c.id for c in people}
    events = bounded(
        s,
        select(SceneRecord)
        .where(
            SceneRecord.story_id == sid,
            SceneRecord.redacted.is_(False),
            SceneRecord.seq.is_not(None),
        )
        .order_by(SceneRecord.seq),
        5000,
        "事件数",
    )
    event_ids = {e.id for e in events}
    observations = bounded(
        s,
        select(Observation).where(Observation.actor_id.in_(actor_ids), Observation.event_id.in_(event_ids)),
        50000,
        "感知记录数",
    )
    observed = defaultdict(set)
    by_event = defaultdict(list)
    for observation in observations:
        observed[observation.actor_id].add(observation.event_id)
        by_event[observation.event_id].append(observation)
    memories = bounded(
        s,
        select(RoleMemory)
        .where(RoleMemory.story_id == sid, RoleMemory.actor_id.in_(actor_ids), RoleMemory.status != "deleted")
        .order_by(RoleMemory.created_at, RoleMemory.id),
        10000,
        "个人记忆数",
    )
    snapshots = bounded(
        s,
        select(RoleSnapshot)
        .where(RoleSnapshot.actor_id.in_(actor_ids))
        .order_by(RoleSnapshot.created_at, RoleSnapshot.id),
        10000,
        "角色快照数",
    )
    people_by_id = {person.id: person for person in people}
    snapshots = [
        snapshot
        for snapshot in snapshots
        if snapshot.privacy_version == people_by_id[snapshot.actor_id].privacy_version
        and set(snapshot.source_ids).issubset(observed[snapshot.actor_id])
    ]
    tasks = bounded(
        s,
        select(Task)
        .where(
            Task.owner_id == who,
            (Task.session_id.in_(actor_ids)) | (Task.payload["story_id"].as_string() == sid),
        )
        .order_by(Task.created_at, Task.id),
        2000,
        "任务数",
    )
    task_ids = {t.id for t in tasks}
    frontier = task_ids
    while frontier:
        descendants = bounded(
            s,
            select(Task).where(
                Task.owner_id == who,
                Task.parent_id.in_(frontier),
                Task.id.not_in(task_ids),
                (Task.session_id.is_(None)) | Task.session_id.in_(actor_ids),
                (Task.payload["story_id"].as_string().is_(None))
                | (Task.payload["story_id"].as_string() == sid),
            ),
            2000,
            "关联任务数",
        )
        frontier = {t.id for t in descendants}
        task_ids.update(frontier)
        tasks.extend(descendants)
        if len(tasks) > 2000:
            raise HTTPException(413, "关联任务总数超过2000，未生成截断档案。")
    tasks.sort(key=lambda t: (t.created_at, t.id))
    usage = bounded(
        s,
        select(Usage)
        .where(Usage.owner_id == who, Usage.task_id.in_(task_ids))
        .order_by(Usage.created_at, Usage.id),
        20000,
        "调用记录数",
    )
    timings = bounded(
        s,
        select(Operation)
        .where(Operation.task_id.in_(task_ids), Operation.id.startswith("timing:"))
        .order_by(Operation.created_at, Operation.id),
        10000,
        "计时记录数",
    )
    timing_map = defaultdict(list)
    for operation in timings:
        raw = operation.payload.get("timing_v1", {})
        # No workset payloads, exception strings, arbitrary attributes or model request bodies.
        timing_map[operation.task_id].append(
            {
                key: raw.get(key)
                for key in (
                    "id",
                    "attempt",
                    "task_status",
                    "started_at",
                    "duration_ms",
                    "initial_queue_ms",
                    "enqueue_to_worker_ms",
                )
            }
        )
    messages = bounded(
        s,
        select(Message)
        .where(
            Message.session_id.in_(actor_ids),
            Message.redacted.is_(False),
            (Message.role == "user") | (Message.delivered.is_(True)),
        )
        .order_by(Message.created_at, Message.id),
        10000,
        "消息数",
    )
    origin = s.get(ExperimentInitial, sid)
    initial_redacted = bool(
        origin
        and s.scalar(
            select(SceneRecord.id)
            .where(
                SceneRecord.story_id == sid,
                SceneRecord.redacted.is_(True),
                SceneRecord.seq <= origin.receipt["initial_seq"],
            )
            .limit(1)
        )
    )
    calls = [
        {
            "id": u.id,
            "task_id": u.task_id,
            "role": u.role,
            "model": u.model,
            "status": u.status,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cache_tokens": u.cache_tokens,
            "cost_usd": u.cost,
            "reserved_usd": u.reserved,
            "duration_ms": u.duration_ms,
            "created_at": u.created_at,
        }
        for u in usage
    ]
    return {
        "format": "sceneweaver-run-record",
        "format_version": 1,
        "captured_at": now(),
        "story": {
            "id": sid,
            "title": story.title,
            "seq": story.seq,
            "paused": bool(story.settings.get("paused")),
            "finished_at": story.settings.get("narrative", {}).get("finished_at"),
            "created_at": story.created_at,
        },
        "initial": {"recipe": origin.recipe, "receipt": origin.receipt, "created_at": origin.created_at}
        if origin and origin.owner_id == who and not initial_redacted
        else None,
        "initial_omitted_for_redaction": initial_redacted,
        "people": [
            {
                "id": c.id,
                "name": c.current_state.get("profile", {}).get("name", c.title),
                "player": bool(c.current_state.get("player")),
                "location": c.current_state.get("location", "main"),
                "revision": c.revision,
            }
            for c in people
        ],
        "events": [
            {
                **event_view(e),
                "task_id": e.task_id,
                "observations": [
                    {"observer_id": o.actor_id, "view": event_view(perceive(e, o), o.actor_id)}
                    for o in by_event[e.id]
                ],
            }
            for e in events
        ],
        "memories": [
            {
                "id": m.id,
                "actor_id": m.actor_id,
                "kind": m.kind,
                "content": m.content,
                "status": m.status,
                "version": m.version,
                "replaces_id": m.replaces_id,
                "source_ids": m.source_ids,
                "created_at": m.created_at,
            }
            for m in memories
            if set(m.source_ids).issubset(observed[m.actor_id])
        ],
        "snapshots": [
            {
                "id": snapshot.id,
                "actor_id": snapshot.actor_id,
                "covered_seq": snapshot.covered_seq,
                "privacy_version": snapshot.privacy_version,
                "summary": snapshot.summary.get("summary", ""),
                "source_ids": snapshot.source_ids,
                "created_at": snapshot.created_at,
            }
            for snapshot in snapshots
        ],
        "messages": [
            {
                "id": m.id,
                "session_id": m.session_id,
                "task_id": m.task_id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at,
            }
            for m in messages
        ],
        "tasks": [
            {
                "id": t.id,
                "parent_id": t.parent_id if t.parent_id in task_ids else None,
                "session_id": t.session_id,
                "kind": t.kind,
                "status": t.status,
                "has_error": bool(t.error),
                "attempt": t.attempt,
                "created_at": t.created_at,
                "updated_at": t.updated_at,
                "actor_id": t.payload.get("step_actor_id")
                if t.payload.get("step_actor_id") in actor_ids
                else None,
                "outcome": (t.result.get("experiment") or {}).get("outcome")
                if not t.result.get("redacted")
                and (t.result.get("experiment") or {}).get("outcome") in {"wait", "committed"}
                else None,
                "timings": timing_map[t.id] if not t.result.get("redacted") else [],
            }
            for t in tasks
        ],
        "calls": calls,
        "cost": {
            "settled_usd": sum(u.cost for u in usage),
            "reserved_usd": sum(u.reserved for u in usage),
            "scope": "剧情任务及其同owner子任务；不包含无法关联的全局后台费用",
        },
        "active_tasks": sum(t.status in {"queued", "running", "waiting"} for t in tasks),
        "limitations": [
            "当前数据库读取档案，不是可恢复快照或完整操作日志。",
            "只保留未删除事件、来源有效的记忆及当前隐私版本快照，不还原历史时点的信念。",
            "未导出原始模型请求、异常正文、音频和详细工作集；运行中费用与计时可能未结算。",
            "供应商内部推理/排队与纯网络时间未知，不能由总耗时精确拆分。",
        ],
    }


class RunTitle(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    expected_title: str


class EventNote(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    source_hash: str = Field(min_length=64, max_length=64)
    category: Literal["boundary", "character", "repetition", "initiative", "good", "other"]
    note: str = Field(default="", max_length=1000)


def archive_router(db, config, owner):
    router = APIRouter(prefix="/api/role/experiments")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    @router.get("/runs")
    def runs(before: str | None = None, blueprint_id: str | None = None, identity=Depends(who)):
        with db.session() as s:
            q = select(Story).where(Story.owner_id == identity)
            if blueprint_id:
                q = q.join(ExperimentInitial).where(
                    ExperimentInitial.owner_id == identity,
                    ExperimentInitial.receipt["blueprint_id"].as_string() == blueprint_id,
                )
            if before:
                cursor = s.get(Story, before)
                if not cursor or cursor.owner_id != identity:
                    raise HTTPException(404, "无效运行游标")
                from sqlalchemy import tuple_

                q = q.where(tuple_(Story.created_at, Story.id) < (cursor.created_at, cursor.id))
            rows = list(s.scalars(q.order_by(Story.created_at.desc(), Story.id.desc()).limit(51)))
            visible = rows[:50]
            note_counts = dict(
                s.execute(
                    select(RunAnnotation.story_id, func.count(RunAnnotation.id))
                    .where(
                        RunAnnotation.owner_id == identity,
                        RunAnnotation.story_id.in_([row.id for row in visible]),
                    )
                    .group_by(RunAnnotation.story_id)
                ).all()
            )
            initial_ids = set(
                s.scalars(
                    select(ExperimentInitial.story_id).where(
                        ExperimentInitial.owner_id == identity,
                        ExperimentInitial.story_id.in_([row.id for row in visible]),
                    )
                )
            )
            return {
                "runs": [
                    {
                        "id": r.id,
                        "title": r.title,
                        "created_at": r.created_at,
                        "paused": bool(r.settings.get("paused")),
                        "finished_at": r.settings.get("narrative", {}).get("finished_at"),
                        "has_initial": r.id in initial_ids,
                        "note_count": note_counts.get(r.id, 0),
                    }
                    for r in visible
                ],
                "next_before": rows[49].id if len(rows) > 50 else None,
            }

    @router.get("/runs/{sid}/record")
    def record(sid: str, identity=Depends(who)):
        with db.session() as s:
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            return archive(s, sid, identity)

    def note_source(s, sid, eid, identity):
        from .experiment_initials import digest

        row = s.scalar(
            select(SceneRecord)
            .join(Story)
            .where(
                Story.id == sid,
                Story.owner_id == identity,
                SceneRecord.id == eid,
                SceneRecord.redacted.is_(False),
            )
            .with_for_update()
        )
        if not row:
            raise HTTPException(404, "事件已不可用或不属于此运行。")
        return digest({"id": row.id, "text": row.text, "payload": row.payload})

    @router.get("/runs/{sid}/events/{eid}/notes")
    def read_notes(sid: str, eid: str, identity=Depends(who)):
        with db.session.begin() as s:
            source_hash = note_source(s, sid, eid, identity)
            rows = s.scalars(
                select(EventAnnotation)
                .where(
                    EventAnnotation.event_id == eid,
                    EventAnnotation.owner_id == identity,
                    EventAnnotation.source_hash == source_hash,
                )
                .order_by(EventAnnotation.created_at.desc())
                .limit(100)
            )
            return {
                "source_hash": source_hash,
                "notes": [
                    dict(id=r.id, category=r.category, note=r.note, created_at=r.created_at) for r in rows
                ],
            }

    @router.post("/runs/{sid}/events/{eid}/notes", status_code=201)
    def add_note(sid: str, eid: str, body: EventNote, identity=Depends(who)):
        with db.session.begin() as s:
            source_hash = note_source(s, sid, eid, identity)
            if source_hash != body.source_hash:
                raise HTTPException(409, "事件内容已变化，请重新查看后标注。")
            old = s.scalar(
                select(EventAnnotation).where(
                    EventAnnotation.event_id == eid,
                    EventAnnotation.owner_id == identity,
                    EventAnnotation.request_id == body.request_id,
                )
            )
            if old:
                if old.category != body.category or old.note != body.note or old.source_hash != source_hash:
                    raise HTTPException(409, "同一次标注重试不能更改内容。")
                return {"id": old.id}
            row = EventAnnotation(
                owner_id=identity,
                event_id=eid,
                request_id=body.request_id,
                source_hash=source_hash,
                category=body.category,
                note=body.note,
            )
            s.add(row)
            s.flush()
            return {"id": row.id}

    @router.patch("/runs/{sid}/title")
    def rename(sid: str, body: RunTitle, identity=Depends(who)):
        if not body.title.strip():
            raise HTTPException(422, "运行名称不能为空")
        with db.session.begin() as s:
            row = s.scalar(select(Story).where(Story.id == sid, Story.owner_id == identity).with_for_update())
            if not row:
                raise HTTPException(404)
            if row.title != body.expected_title:
                raise HTTPException(409, "运行名称已变化，请刷新后重试")
            row.title = body.title.strip()
            return {"title": row.title}

    return router
