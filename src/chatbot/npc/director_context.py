"""Bounded common perception, including public dialogue links, never private intent."""

import json

from sqlalchemy import select

from ..storage import InteractionRequest, RequestTransition


def reply_links(session, event_ids):
    """One bounded query for immutable speech-to-question links in this window."""
    links = {}
    for transition, target in session.execute(select(RequestTransition, InteractionRequest.target_id).join(
        InteractionRequest, InteractionRequest.event_id == RequestTransition.request_id
    ).where(RequestTransition.event_id.in_(event_ids))):
        links.setdefault(transition.event_id, []).append({
            "target": target, "question_id": transition.request_id, "status": transition.status,
        })
    return links


def event_view(record, links=None):
    event = {
        "event_id": record.id,
        "kind": record.kind,
        "actor_id": record.actor_id,
        "text": record.text,
        "addressees": list(record.addressees or []),
    }
    # These are committed delivery facts. The actor's purpose, working set and
    # other payload fields must never enter the director's common context.
    if record.kind == "speech" and not record.payload.get("perception"):
        exchange = record.payload.get("exchange") or {}
        if links is not None and not exchange.get("reply_to"):
            replies = [link for link in links.get(record.id, []) if link["target"] == record.actor_id]
            if len(replies) == 1:
                exchange = {**exchange, "reply_to": replies[0]["question_id"], "response_kind": replies[0]["status"]}
        event["exchange"] = {
            key: exchange[key]
            for key in ("expects_reply", "target_id", "reply_to", "response_kind")
            if key in exchange
        }
        update = record.payload.get("exchange_update") or {}
        event["exchange_update"] = {key: update[key] for key in ("question_id", "status") if key in update}
    return event


def common_events(views):
    if not views:
        return []
    return [
        event
        for event_id, event in views[0].items()
        if all(view.get(event_id) == event for view in views[1:])
    ]


def dialogue_packet(cast, events):
    """Recompute links after clipping; open only means no shared observed reply."""
    known = {person["id"] for person in cast}
    retained = {event["event_id"] for event in events}
    safe_events, exchanges = [], {}
    for event in events:
        safe = {**event, "addressees": [actor for actor in event["addressees"] if actor in known]}
        delivery = dict(event.get("exchange", {}))
        update = dict(event.get("exchange_update", {}))
        if delivery.get("target_id") not in known:
            delivery.pop("target_id", None)
        if delivery.get("reply_to") not in retained:
            delivery.pop("reply_to", None)
        if update.get("question_id") not in retained:
            update = {}
        if delivery:
            safe["exchange"] = delivery
        else:
            safe.pop("exchange", None)
        if update:
            safe["exchange_update"] = update
        else:
            safe.pop("exchange_update", None)
        safe_events.append(safe)
        for request_id, status, author in (
            (update.get("question_id"), update.get("status"), "asker_id"),
            (delivery.get("reply_to"), delivery.get("response_kind", "answer"), "target_id"),
        ):
            request = exchanges.get(request_id)
            if (
                request
                and event["actor_id"] == request[author]
                and status in {"open", "cancelled", "answer", "partial", "defer", "refuse", "silence"}
            ):
                request.update(status=status, response_event_id=event["event_id"])
        target = delivery.get("target_id")
        if (
            delivery.get("expects_reply")
            and target
            and event["actor_id"] in known
            and target != event["actor_id"]
        ):
            exchanges[event["event_id"]] = {
                "question_id": event["event_id"],
                "asker_id": event["actor_id"],
                "target_id": target,
                "status": "open",
                "response_event_id": None,
            }
    return {
        "cast": cast,
        "events": safe_events,
        "exchanges": list(exchanges.values()),
        "exchange_status_scope": "shared_observed_window",
    }


def bounded_public_context(cast, events, *, max_chars=15000):
    window = list(events)
    while True:
        encoded = json.dumps(dialogue_packet(cast, window), ensure_ascii=False)
        if not window or len(encoded) <= max_chars:
            return encoded
        window.pop(0)
