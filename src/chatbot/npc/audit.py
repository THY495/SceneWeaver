"""Identifier provenance for prepared NPC inputs; never changes model-visible data."""

import json

from ..observability.timing import audit_message
from ..role_policy import review_grounding


def review_message(info, evidence, speech, sources, *, actor_id, documents):
    grounding = review_grounding(info, evidence, speech, sources)
    primary = grounding["primary_grounding"]
    return audit_message(
        {"role": "user", "content": json.dumps(grounding, ensure_ascii=False)},
        section="review_grounding",
        groups={
            "profile": [actor_id],
            "documents": [e["id"] for e in documents],
            "references": [e["id"] for e in grounding["referenced_records_with_provenance"]],
            "recent_utterances": [e["id"] for e in grounding["recent_observed_utterances"]],
            "confirmed_events": [v.get("event_id") for v in primary["confirmed_events"].values()],
            "item_action_results": [e["id"] for e in primary["item_action_results"]],
            "subjective_goals": list(grounding["subjective_goals_not_completed_actions"]),
            "player_statements": [
                e["id"] for e in (grounding.get("player_statement_history") or {}).get("items", [])
            ],
        },
    )


def retrieval_message(message, retrieved, selected):
    selected_ids = {e["id"] for e in selected}
    return audit_message(
        message,
        section="retrieval_evidence",
        groups={
            "documents": [e["id"] for e in selected],
            "capacity_omitted": [e["id"] for e in retrieved if e["id"] not in selected_ids],
            "excerpt_truncated": [e["id"] for e in selected if e.get("excerpt_truncated")],
        },
    )
