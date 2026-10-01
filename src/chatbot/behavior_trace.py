"""Creator-only identifier/timing audit; never consumed as character context."""

from sqlalchemy import select

from .observability.timing import read_timings
from .storage import (
    ActionAttempt,
    Event,
    Observation,
    Operation,
    SceneRecord,
    Usage,
    VoiceDelivery,
    VoicePlayback,
)


def behavior_trace(s, task):
    routing = s.get(Operation, f"scene-routing-audit:{task.id}")
    records = list(
        s.scalars(
            select(SceneRecord)
            .where(SceneRecord.task_id == task.id, SceneRecord.redacted.is_(False))
            .order_by(SceneRecord.seq)
        )
    )
    ids = [event.id for event in records]
    observations = list(s.scalars(select(Observation).where(Observation.event_id.in_(ids)))) if ids else []
    deliveries = list(s.scalars(select(VoiceDelivery).where(VoiceDelivery.event_id.in_(ids)))) if ids else []
    playbacks = list(s.scalars(select(VoicePlayback).where(VoicePlayback.event_id.in_(ids)))) if ids else []
    usage = list(s.scalars(select(Usage).where(Usage.task_id == task.id).order_by(Usage.created_at)))
    notifications = list(s.scalars(select(Event).where(Event.task_id == task.id).order_by(Event.id)))
    return {
        "scene_routing": routing.payload if routing else None,
        "timings": read_timings(s, task.id),
        "action_attempts": [
            {
                "id": attempt.id,
                "actor_id": attempt.actor_id,
                "item_id": attempt.item_id,
                "status": attempt.status,
                "task_lease_version": attempt.task_lease_version,
                "pause_reason": attempt.pause_reason,
                "retry_at": attempt.retry_at.isoformat() if attempt.retry_at else None,
                "result_event_id": attempt.result_event_id,
            }
            for attempt in s.scalars(select(ActionAttempt).where(ActionAttempt.task_id == task.id))
        ],
        "task_id": task.id,
        "kind": task.kind,
        "status": task.status,
        "created_at": task.created_at.isoformat(),
        "attempt": task.attempt,
        "lease_version": task.lease_version,
        "trigger_event_id": task.payload.get("event_id"),
        "autonomous": bool(task.payload.get("autonomous")),
        "events": [
            {
                "event_id": event.id,
                "story_id": event.story_id,
                "scene": event.scene,
                "seq": event.seq,
                "actor_id": event.actor_id,
                "kind": event.kind,
                "committed_at": event.created_at.isoformat(),
                "agreement": event.payload.get("agreement"),
                "source_ids": event.payload.get("context_source_ids", []),
                "observations": [
                    {"actor_id": obs.actor_id, "event_id": obs.event_id, "restricted": bool(obs.perception)}
                    for obs in observations
                    if obs.event_id == event.id
                ],
                "text_delivery": [
                    {"actor_id": d.session_id, "visible": d.text_visible}
                    for d in deliveries
                    if d.event_id == event.id
                ],
                "playback": [
                    {
                        "playback_id": p.id,
                        "actor_id": p.session_id,
                        "status": p.status,
                        "sent_samples": p.sent_samples,
                        "played_samples": p.played_samples,
                        "timings": p.timings,
                    }
                    for p in playbacks
                    if p.event_id == event.id
                ],
            }
            for event in records
        ],
        "notifications": [
            {
                "id": event.id,
                "session_id": event.session_id,
                "kind": event.kind,
                "created_at": event.created_at.isoformat(),
            }
            for event in notifications
        ],
        "calls": [
            {
                "id": call.id,
                "role": call.role,
                "model": call.model,
                "status": call.status,
                "duration_ms": call.duration_ms,
                "cost_usd": call.cost,
                "reserved_usd": call.reserved,
            }
            for call in usage
        ],
        "cost_usd": sum(call.cost for call in usage),
        "unavailable": [
            "provider_queue_ms",
            "provider_inference_ms",
            "dns_tcp_tls_ms",
            "transaction_only_ms",
            "browser_server_clock_sync",
            "live_spans",
        ],
        "note": "创作审计视图，不可输入角色上下文；未采集的指标保持未知。",
    }
