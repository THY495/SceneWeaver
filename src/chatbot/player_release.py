"""Player delivery grants. A grant is not a world event or proof of hearing."""

from sqlalchemy import select

from .observations import record_data
from .storage import Observation, Operation, SceneRecord, Task, now


def pending_tasks(s, player):
    if not player.current_state.get("player"):
        return set()
    records = s.scalars(
        select(SceneRecord)
        .join(Task, SceneRecord.task_id == Task.id)
        .join(Observation, Observation.event_id == SceneRecord.id)
        .where(
            Task.session_id == player.id,
            Task.epoch == player.epoch,
            Observation.actor_id == player.id,
            SceneRecord.redacted.is_(False),
        )
    )
    return {r.task_id for r in records if pending(s, player, r)}


def withhold(payload):
    # Aggregated answers can contain more than the current segment. Never release
    # them piecemeal by guessing which substrings belong to an approved delivery.
    return {
        k: v
        for k, v in payload.items()
        if k in {"status", "route", "utterance_id", "actor_id", "incremental", "stop_reason"}
    } | {"presentation_pending": True}


def pending(s, player, record):
    """Only new voice-ready publications in this player's current turn are gated."""
    policy = record.payload.get("presentation", {})
    if policy.get("audio_status") != "ready" or record.actor_id == player.id:
        return False
    task = s.get(Task, record.task_id) if record.task_id else None
    if not task or task.session_id != player.id or task.epoch != player.epoch:
        return False
    return s.get(Operation, f"player-release:{player.id}:{player.epoch}:{record.id}") is None


def project(s, player, record, *, audio=True):
    if audio and pending(s, player, record):
        # Allow selecting the next reading slot without leaking speech, expression,
        # appearance, addressees or other performance metadata before release.
        return {
            "id": record.id,
            "seq": record.seq,
            "kind": record.kind,
            "actor_id": record.actor_id,
            "speaker": record.actor_name,
            "text": "",
            "presentation_pending": True,
        }
    return record_data(record, viewer_id=player.id)


def grant(s, player, record, *, reason, playback_id=None):
    """Caller holds the player lock and has checked observation and readiness.

    Keep only delivery metadata in the ledger. Re-project the observed record on
    every response so subsequent redaction or partial-hearing changes still apply.
    """
    key = f"player-release:{player.id}:{player.epoch}:{record.id}"
    row = s.get(Operation, key)
    if row is None:
        row = Operation(
            id=key,
            task_id=record.task_id or "",
            payload={
                "version": 1,
                "release_id": record.id,
                "session_id": player.id,
                "epoch": player.epoch,
                "reason": reason,
                "playback_id": playback_id,
                "released_at": now().isoformat(),
            },
        )
        s.add(row)
    return {**row.payload, "event": record_data(record, viewer_id=player.id)}
