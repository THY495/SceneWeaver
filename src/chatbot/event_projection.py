"""Rebuildable typed indices. Event text and observer access remain the sole authority."""

from sqlalchemy import exists, or_, select

from .provenance import source_class
from .storage import EventAuthority, InteractionRequest, RequestTransition, SceneRecord, SpeechAct, Story


def project_event(s, event):
    # Idempotent under the story lock held by append_record/backfill.
    if event.redacted:
        return
    authority = s.get(EventAuthority, event.id)
    classification = source_class({"kind": event.kind, "detail": event.payload})
    if authority:
        authority.source_class = classification
    else:
        s.add(EventAuthority(event_id=event.id, source_class=classification))
    if event.kind == "speech" and not s.get(SpeechAct, event.id):
        s.add(SpeechAct(event_id=event.id, speaker_id=event.actor_id))
    exchange = event.payload.get("exchange") or {}
    # Older releases attached a request to an object action. Preserve that request's
    # history without reclassifying the action as speech or generating a new utterance.
    if event.actor_id and exchange.get("expects_reply"):
        target = exchange.get("target_id")
        from .storage import Conversation

        recipient = s.get(Conversation, target) if target else None
        if recipient and recipient.story_id == event.story_id and target != event.actor_id:
            if not s.get(InteractionRequest, event.id):
                s.add(InteractionRequest(event_id=event.id, asker_id=event.actor_id, target_id=target))
                s.flush()
    update = event.payload.get("exchange_update") or {}
    changes = [
        (update.get("question_id"), update.get("status"), "asker_id"),
        (exchange.get("reply_to"), exchange.get("response_kind", "answer"), "target_id"),
    ]
    for request_id, status, author in changes:
        if not request_id or status not in {
            "open",
            "cancelled",
            "answer",
            "partial",
            "defer",
            "refuse",
            "silence",
        }:
            continue
        origin = s.get(SceneRecord, request_id)
        if origin and not origin.redacted and origin.story_id == event.story_id and origin.seq < event.seq:
            if not s.get(InteractionRequest, request_id):
                project_event(s, origin)
        request = s.get(InteractionRequest, request_id)
        if (
            request
            and origin
            and not origin.redacted
            and origin.story_id == event.story_id
            and origin.seq < event.seq
            and getattr(request, author) == event.actor_id
            and not s.get(RequestTransition, (event.id, request_id))
        ):
            s.add(RequestTransition(event_id=event.id, request_id=request_id, status=status))


def backfill_story(s, story_id, *, after=0, through=None, limit=250):
    """Bounded pages with a caller-owned fixed watermark; no model or world changes."""
    if not 1 <= limit <= 1000:
        raise ValueError("Invalid projection batch size")
    story = s.scalar(select(Story).where(Story.id == story_id).with_for_update())
    if not story:
        raise KeyError("Story not found")
    through = story.seq if through is None else min(through, story.seq)
    rows = list(
        s.scalars(
            select(SceneRecord)
            .where(SceneRecord.story_id == story_id, SceneRecord.seq > after, SceneRecord.seq <= through)
            .order_by(SceneRecord.seq)
            .limit(limit)
        )
    )
    for event in rows:
        project_event(s, event)
    return {"through": through, "after": rows[-1].seq if rows else after, "processed": len(rows)}


def projected_exchanges(s, actor_id):
    """Never expose global latest status: a private reply can be unknown to this viewer."""
    from .storage import Observation

    records = list(
        s.scalars(
            select(SceneRecord)
            .join(Observation)
            .where(
                Observation.actor_id == actor_id,
                Observation.perception == {},
                SceneRecord.redacted.is_(False),
                or_(
                    exists().where(InteractionRequest.event_id == SceneRecord.id),
                    exists().where(RequestTransition.event_id == SceneRecord.id),
                ),
            )
            .order_by(SceneRecord.seq)
        )
    )
    ids = [r.id for r in records]
    requests = {
        r.event_id: r
        for r in s.scalars(select(InteractionRequest).where(InteractionRequest.event_id.in_(ids)))
    }
    transitions = {}
    for transition in s.scalars(select(RequestTransition).where(RequestTransition.event_id.in_(ids))):
        transitions.setdefault(transition.event_id, []).append(transition)
    pending = {}
    for event in records:
        for transition in transitions.get(event.id, []):
            item = pending.get(transition.request_id)
            if not item:
                continue
            if event.actor_id == item["asker_id"] or (
                event.actor_id == item["target_id"] and item["status"] in {"open", "partial", "defer"}
            ):
                item.update(status=transition.status, response_event_id=event.id)
        request = requests.get(event.id)
        if request:
            pending[event.id] = {
                "question_id": event.id,
                "asker_id": request.asker_id,
                "target_id": request.target_id,
                "question": event.text,
                "status": "open",
                "response_event_id": None,
                "task_id": event.task_id,
            }
    return list(pending.values())


def requests_for(s, actor_id):
    """Switch only stories whose entire prior history passed the migration audit."""
    from .observations import visible_records
    from .scene_state import exchanges
    from .storage import Conversation

    actor = s.get(Conversation, actor_id)
    story = s.get(Story, actor.story_id)
    if story.settings.get("request_projection_version") == 1:
        return projected_exchanges(s, actor_id)
    return exchanges(visible_records(s, actor_id))
