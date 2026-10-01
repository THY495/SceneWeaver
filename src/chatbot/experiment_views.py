"""Read-only, bounded observation frames for the creator's experiment workbench."""

from fastapi import HTTPException
from sqlalchemy import select

from .event_projection import requests_for
from .observations import perceive, record_data
from .storage import (
    Conversation,
    InteractionRequest,
    Observation,
    Operation,
    RequestTransition,
    RoleMemory,
    RoleSnapshot,
    SceneRecord,
    Story,
    Task,
    now,
)


def _exchange_views(s, actor_id, visible_ids):
    items = requests_for(s, actor_id)
    request_ids = [item["question_id"] for item in items]
    transitions = list(
        s.scalars(
            select(RequestTransition).where(
                RequestTransition.request_id.in_(request_ids),
                RequestTransition.event_id.in_(visible_ids),
            )
        )
    )
    event_ids = {item["question_id"] for item in items} | {row.event_id for row in transitions}
    records = {row.id: row for row in s.scalars(select(SceneRecord).where(SceneRecord.id.in_(event_ids)))}
    by_request = {}
    for row in transitions:
        event = records.get(row.event_id)
        if event:
            by_request.setdefault(row.request_id, []).append(
                {"status": row.status, "event_id": row.event_id, "seq": event.seq}
            )
    result = []
    for item in items:
        origin = records.get(item["question_id"])
        history = [{"status": "open", "event_id": item["question_id"], "seq": origin.seq}] if origin else []
        history.extend(sorted(by_request.get(item["question_id"], []), key=lambda row: row["seq"]))
        result.append({**item, "transitions": history})
    return result


def _exchange_matrix(s, story_id):
    people = list(
        s.scalars(
            select(Conversation)
            .where(Conversation.story_id == story_id)
            .order_by(Conversation.created_at, Conversation.id)
        )
    )
    records = list(
        s.scalars(
            select(SceneRecord)
            .join(InteractionRequest, InteractionRequest.event_id == SceneRecord.id)
            .where(SceneRecord.story_id == story_id, SceneRecord.redacted.is_(False))
            .order_by(SceneRecord.seq.desc())
            .limit(50)
        )
    )
    visible = {
        person.id: {item["question_id"]: item for item in requests_for(s, person.id)} for person in people
    }
    requests = {
        row.event_id: row
        for row in s.scalars(
            select(InteractionRequest).where(InteractionRequest.event_id.in_([event.id for event in records]))
        )
    }
    names = {person.id: person.current_state.get("profile", {}).get("name", "") for person in people}
    return [
        {
            "question_id": event.id,
            "seq": event.seq,
            "question": event.text,
            "asker_id": requests[event.id].asker_id,
            "asker_name": names.get(requests[event.id].asker_id, ""),
            "target_id": requests[event.id].target_id,
            "target_name": names.get(requests[event.id].target_id, ""),
            "views": [
                {
                    "actor_id": person.id,
                    "actor_name": names[person.id],
                    "player": bool(person.current_state.get("player")),
                    "status": visible[person.id].get(event.id, {}).get("status", "unseen"),
                    "response_event_id": visible[person.id].get(event.id, {}).get("response_event_id"),
                }
                for person in people
            ],
        }
        for event in reversed(records)
    ]


def _latest_context_use(s, story_id, owner, actor_id):
    actor_ids = list(s.scalars(select(Conversation.id).where(Conversation.story_id == story_id)))
    tasks = list(
        s.scalars(select(Task).where(Task.owner_id == owner).order_by(Task.created_at.desc()).limit(100))
    )
    tasks = [
        task for task in tasks if task.payload.get("story_id") == story_id or task.session_id in actor_ids
    ][:50]
    by_id = {task.id: task for task in tasks}
    if not by_id:
        return None
    operations = s.scalars(
        select(Operation)
        .where(Operation.task_id.in_(by_id), Operation.id.startswith("timing:"))
        .order_by(Operation.created_at.desc())
        .limit(100)
    )
    for operation in operations:
        trace = operation.payload.get("timing_v1", {})
        for workset in reversed(trace.get("worksets", [])):
            if workset.get("actor_id") != actor_id:
                continue
            task = by_id[operation.task_id]
            return {
                "task_id": task.id,
                "task_kind": task.kind,
                "task_status": task.status,
                "recorded_at": operation.created_at,
                "snapshot_id": workset.get("snapshot_id"),
                "actor_revision": workset.get("revision"),
                "privacy_version": workset.get("privacy_version"),
                "estimated_tokens": workset.get("estimated_tokens"),
                "observation_count": len(workset.get("observation_ids", [])),
                "memory_count": len(workset.get("memory_ids", [])),
                "omitted_id_count": workset.get("omitted_id_count", 0),
                "scope": workset.get("scope"),
            }
    return None


def observation_frame(s, story_id, owner, actor_id=None):
    story = s.scalar(select(Story).where(Story.id == story_id, Story.owner_id == owner))
    if not story:
        raise HTTPException(404, "剧情不存在")
    viewer = s.get(Conversation, actor_id) if actor_id else None
    if actor_id and (not viewer or viewer.story_id != story_id or viewer.owner_id != owner):
        raise HTTPException(404, "观察角色不属于当前剧情")
    q = select(SceneRecord).where(
        SceneRecord.story_id == story_id,
        SceneRecord.redacted.is_(False),
        SceneRecord.seq.is_not(None),
    )
    if viewer:
        q = q.join(Observation).where(Observation.actor_id == actor_id)
    # IDs establish source validity across the archive, not just the displayed 100 events.
    visible_ids = set(s.scalars(q.with_only_columns(SceneRecord.id)))
    rows = list(s.scalars(q.order_by(SceneRecord.seq.desc()).limit(100)))
    events = [
        record_data(
            perceive(row, s.get(Observation, (actor_id, row.id))) if viewer else row,
            privileged=viewer is None,
            viewer_id=actor_id,
        )
        for row in reversed(rows)
    ]
    memory_ids = []
    if viewer:
        candidates = s.execute(
            select(RoleMemory.id, RoleMemory.source_ids)
            .where(RoleMemory.actor_id == actor_id, RoleMemory.status != "deleted")
            .order_by(RoleMemory.created_at.desc(), RoleMemory.id.desc())
        )
        memory_ids = [mid for mid, sources in candidates if set(sources).issubset(visible_ids)]
    memories = list(s.scalars(select(RoleMemory).where(RoleMemory.id.in_(memory_ids[:100]))))
    by_id = {m.id: m for m in memories}
    snapshot = None
    if viewer:
        candidate = s.scalar(
            select(RoleSnapshot)
            .where(
                RoleSnapshot.actor_id == actor_id,
                RoleSnapshot.privacy_version == viewer.privacy_version,
            )
            .order_by(RoleSnapshot.covered_seq.desc(), RoleSnapshot.created_at.desc())
            .limit(1)
        )
        if candidate and set(candidate.source_ids).issubset(visible_ids):
            snapshot = {
                "id": candidate.id,
                "covered_seq": candidate.covered_seq,
                "privacy_version": candidate.privacy_version,
                "summary": candidate.summary.get("summary", ""),
                "source_ids": candidate.source_ids,
                "created_at": candidate.created_at,
            }
    return {
        "story_id": story_id,
        "actor_id": actor_id,
        "captured_at": now().isoformat(),
        "story_seq": story.seq,
        "paused": bool(story.settings.get("paused")),
        "actor": {
            "name": viewer.current_state.get("profile", {}).get("name", ""),
            "location": viewer.current_state.get("location", "main"),
            "revision": viewer.revision,
            "epoch": viewer.epoch,
        }
        if viewer
        else None,
        "events": events,
        "event_total": len(visible_ids),
        "memories": [
            {
                "id": m.id,
                "kind": m.kind,
                "content": m.content,
                "status": m.status,
                "version": m.version,
                "replaces_id": m.replaces_id,
                "source_ids": m.source_ids,
            }
            for mid in memory_ids[:100]
            if (m := by_id.get(mid))
        ],
        "memory_total": len(memory_ids),
        "snapshot": snapshot,
        "exchanges": _exchange_views(s, actor_id, visible_ids) if viewer else [],
        "latest_context_use": _latest_context_use(s, story_id, owner, actor_id) if viewer else None,
        "exchange_matrix": _exchange_matrix(s, story_id),
        "limit": 100,
    }
