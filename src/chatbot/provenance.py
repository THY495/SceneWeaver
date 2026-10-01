"""Source authority is assigned by event type, never by an actor's claim mode."""


def source_class(event):
    kind = event.get("kind")
    detail = event.get("detail") or {}
    if kind == "speech":
        return "utterance"
    if kind == "scene_change":
        # The scene runtime commits location changes as world events. Their
        # occurrence is stronger evidence than an actor saying they moved.
        return "confirmed_event"
    if kind == "host":
        return "confirmed_event" if detail.get("basis") in {"confirmed", "witnessed"} else "host_claim"
    if kind == "memory_change":
        return "subjective_state"
    if kind == "object_state":
        return "object_observation"
    if kind == "action" and (detail.get("item_action_result") or {}).get("status") in {
        "completed",
        "rejected",
    }:
        return "item_action_result"
    if kind == "relationship":
        return "relationship_event"
    if kind == "authoring":
        return "authoring_event"
    return "unclassified"


def cannot_establish_objective_claim(event):
    return source_class(event) in {"utterance", "host_claim", "subjective_state", "unclassified"}
