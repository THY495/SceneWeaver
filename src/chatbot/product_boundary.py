"""Reject cross-product session state before assembling model input."""

ROLE_STATE_KEYS = frozenset(
    {
        "profile",
        "player",
        "character_origin",
        "scene_facts",
        "scene_claims",
        "unnoticed_ids",
        "known_objects",
        "known_agreements",
        "direction_progress",
    }
)


def has_role_state(conversation) -> bool:
    return bool(conversation.story_id or ROLE_STATE_KEYS.intersection(conversation.current_state or {}))
