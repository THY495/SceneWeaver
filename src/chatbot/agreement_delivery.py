"""Transactional notification outbox. Delivery never grants an observation."""

from sqlalchemy import select

from .storage import AgreementEvent, AgreementNotice, Conversation, Observation, SceneRecord, Story


def record_agreement(s, record):
    value = record.payload.get("agreement") or {}
    if value.get("type") not in {"proposal", "decision", "completion", "attempt_failed"}:
        return
    if not s.get(AgreementEvent, record.id):
        s.add(
            AgreementEvent(
                event_id=record.id,
                proposal_id=value["proposal_id"],
                version=value["version"],
                kind=value["type"],
            )
        )
    if value["type"] == "proposal":
        story = s.get(Story, record.story_id)
        story.settings = {**story.settings, "conditional_actions_version": 1}
    for observation in s.scalars(select(Observation).where(Observation.event_id == record.id)):
        if not observation.perception and not s.get(AgreementNotice, (record.id, observation.actor_id)):
            s.add(AgreementNotice(event_id=record.id, actor_id=observation.actor_id))


def deliver_notices(db, runtime, limit=32):
    """One short transaction per notice; same lock order as world commits, restart safe."""
    with db.session() as s:
        scope = getattr(runtime, "execution_scope", None)
        keys = [
            (row.event_id, row.actor_id)
            for row in s.scalars(
                select(AgreementNotice)
                .where(
                    AgreementNotice.status == "pending",
                    AgreementNotice.actor_id.in_(scope.actors()) if scope else True,
                    AgreementNotice.event_id.in_(
                        select(SceneRecord.id).where(SceneRecord.story_id.in_(scope.stories()))
                    )
                    if scope
                    else True,
                )
                .order_by(AgreementNotice.event_id, AgreementNotice.actor_id)
                .limit(limit)
            )
        ]
    count = 0
    for eid, aid in keys:
        with db.session.begin() as s:
            record = s.get(SceneRecord, eid)
            s.scalar(select(Story).where(Story.id == record.story_id).with_for_update())
            actor = s.scalar(select(Conversation).where(Conversation.id == aid).with_for_update())
            notice = s.get(AgreementNotice, (eid, aid), with_for_update=True)
            s.refresh(record)
            if notice.status != "pending":
                continue
            observation = s.get(Observation, (aid, eid))
            if record.redacted or not observation or observation.perception:
                notice.status = "cancelled"
                continue
            event = runtime.emit(s, actor, "agreement_updated", {"message": "已知行动事项已更新"})
            s.flush()
            notice.notification_id, notice.status = event.id, "delivered"
            count += 1
    return count
