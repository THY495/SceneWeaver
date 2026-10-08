"""Authored interaction requirement; only committed common observations count."""

from sqlalchemy import select

from ..storage import Operation, SceneRecord, Story


def beat_committed_event_ids(session, task_id, beat_id):
    ledgers = list(session.scalars(select(Operation).where(Operation.id.startswith(f"{task_id}:director-ledger"))))
    result = {
        candidate["event_id"]
        for ledger in ledgers
        if ledger.payload.get("plan", {}).get("beat_id") == beat_id
        for candidate in ledger.payload.get("candidates", {}).values()
        if candidate.get("status") == "committed" and candidate.get("event_id")
    }
    for ledger in ledgers:
        data = ledger.payload
        if data.get("plan", {}).get("beat_id") != beat_id or not data.get("inherited_event_ids"):
            continue
        story = session.get(Story, data.get("story_id"))
        if not story:
            continue
        result.update(session.scalars(select(SceneRecord.id).where(
            SceneRecord.id.in_(data["inherited_event_ids"]), SceneRecord.story_id == story.id,
            SceneRecord.redacted.is_(False), SceneRecord.seq <= data["world_seq"],
        )))
    return result


def npc_exchange_observed(beat, events, current_event_ids):
    participants = set(beat.participants)
    return any(
        event["event_id"] in current_event_ids
        and event["kind"] == "speech"
        and event["actor_id"] in participants
        and any(
            target in participants and target != event["actor_id"] for target in event.get("addressees", [])
        )
        for event in events
    )
