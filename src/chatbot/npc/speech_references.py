"""Repair one unambiguous identifier alias; semantic support is still reviewed."""

from copy import deepcopy


def normalize_own_profile_reference(speech, actor_id, sources):
    """An actor UUID can name only its own registered profile, with an exact quote.

    Never search other profiles or match fuzzy excerpts. Unknown identifiers,
    wrong quotes and collisions stay invalid for the ordinary repair pipeline.
    Text, mode and quoted content remain unchanged; this grants no fact support.
    """
    canonical = f"profile:{actor_id}"
    body = sources.get(canonical)
    if not body or actor_id in sources:
        return speech, 0
    claims = speech.get("claims", [])
    indexes = [i for i, claim in enumerate(claims)
               if claim.get("source_id") == actor_id and claim.get("quote")
               and claim["quote"] in body and claim.get("text")
               and claim["text"] in speech["text"]]
    if not indexes:
        return speech, 0
    repaired = deepcopy(speech)
    for index in indexes:
        repaired["claims"][index]["source_id"] = canonical
    return repaired, len(indexes)
