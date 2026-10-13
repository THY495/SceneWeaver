"""Small independent indexing jobs; summaries never own the embedding backlog."""

from datetime import UTC, timedelta

from sqlalchemy import func, or_, select, text

from .memory_dependencies import memory_visible
from .observations import visible_records
from .providers import Embeddings
from .storage import Conversation, Observation, RoleMemory, SceneRecord, Task, Usage, now


def schedule_index(db, config, runtime):
    scope = getattr(runtime, "execution_scope", None)
    if config.embedding_provider == "lexical":
        return None
    with db.session.begin() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(731292)"))
        day = now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        spent = s.scalar(
            select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)).where(Usage.created_at >= day)
        )
        if spent >= config.daily_budget_usd * 0.9:
            return None
        # One small maintenance job at a time, with backoff even after provider failure.
        recent = s.scalar(
            select(Task.id).where(
                scope.tasks() if scope else True,
                Task.kind == "role_index",
                or_(
                    Task.status.in_(["queued", "running"]),
                    (Task.status.in_(["failed", "cancelled", "superseded"]))
                    & (Task.created_at > now() - timedelta(minutes=2)),
                ),
            )
        )
        if recent:
            return None
        rows = list(
            s.scalars(
                select(RoleMemory)
                .where(
                    RoleMemory.actor_id.in_(scope.actors()) if scope else True,
                    RoleMemory.kind == "episode",
                    RoleMemory.status == "active",
                    or_(RoleMemory.embedding.is_(None), RoleMemory.embedding_model != config.embedding_model),
                )
                .order_by(RoleMemory.created_at, RoleMemory.id)
                .limit(config.embedding_batch_size)
            )
        )
        if not rows:
            return None
        actor = s.get(Conversation, rows[0].actor_id)
        rows = [r for r in rows if r.actor_id == actor.id]
        return runtime.enqueue(
            s,
            "role_index",
            actor.owner_id,
            {"ids": [r.id for r in rows], "privacy": actor.privacy_version},
            session_id=actor.id,
            priority=90,
        )


async def index_memories(agents, task):
    with agents.db.session() as s:
        actor = s.get(Conversation, task.session_id)
        if actor.privacy_version != task.payload["privacy"]:
            return {"stale": True}
        rows = list(
            s.scalars(
                select(RoleMemory).where(
                    RoleMemory.id.in_(task.payload["ids"]),
                    RoleMemory.actor_id == actor.id,
                    RoleMemory.status == "active",
                )
            )
        )
        selected = []
        visible = {event.id for event in visible_records(s, actor.id)}
        for row in rows:
            if not memory_visible(row, visible):
                continue
            sources = [s.get(SceneRecord, eid) for eid in row.source_ids]
            seen = set(
                s.scalars(
                    select(Observation.event_id).where(
                        Observation.actor_id == actor.id, Observation.event_id.in_(row.source_ids)
                    )
                )
            )
            if sources and all(e and not e.redacted and e.id in seen for e in sources):
                selected.append((row.id, row.version, row.content))
    if not selected:
        return {"indexed": 0}
    vectors = await Embeddings(agents.config, agents.provider).encode([r[2] for r in selected], task.id)
    if vectors is None:
        return {"indexed": 0}
    agents.runtime.check(task.id, task.lease_version)
    installed = 0
    with agents.db.session.begin() as s:
        actor = s.scalar(select(Conversation).where(Conversation.id == task.session_id).with_for_update())
        # The outside check avoids unnecessary work but cannot fence writes.
        # Hold the original lease through installation, after the actor lock.
        current = s.scalar(select(Task).where(Task.id == task.id).with_for_update())
        if (
            not actor
            or actor.owner_id != task.owner_id
            or actor.privacy_version != task.payload["privacy"]
            or not current
            or current.session_id != actor.id
            or current.owner_id != task.owner_id
            or current.kind != "role_index"
            or current.status != "running"
            or current.lease_version != task.lease_version
            or not current.lease_until
            or current.lease_until <= now()
            or current.deadline <= now()
        ):
            return {"stale": True}
        visible = {event.id for event in visible_records(s, actor.id)}
        for (mid, version, content), vector in zip(selected, vectors or [], strict=True):
            row = s.get(RoleMemory, mid)
            if (row and row.status == "active" and row.version == version and row.content == content
                and memory_visible(row, visible)):
                row.embedding = vector
                row.embedding_model = agents.config.embedding_model
                installed += 1
    return {"indexed": installed}
