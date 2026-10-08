"""Correct a speech source mistaken for a question, without changing intent."""


def normalize_statement_reference(intent, info):
    """Clear only a known non-question link when there is no unfinished reply.

    Call only after all other intention checks pass. The source remains cited;
    this is neither a new answer nor completion of a dialogue obligation.
    """
    reference_id = intent.get("reply_to")
    if (
        not reference_id
        or intent.get("act") != "speak"
        or intent.get("response_kind") != "answer"
        or not intent.get("addressee_id")
        or info.get("pending_replies") != []
        or reference_id not in intent.get("source_ids", [])
    ):
        return intent
    reference = next(
        (event for event in info.get("observations", []) if event.get("id") == reference_id), None
    )
    if (
        not reference
        or reference.get("kind") != "speech"
        or reference.get("actor_id") != intent["addressee_id"]
        or reference.get("perception")
        or reference.get("exchange", {}).get("expects_reply") is not False
    ):
        return intent
    return {**intent, "reply_to": None}
