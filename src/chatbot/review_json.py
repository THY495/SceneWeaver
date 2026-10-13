"""One syntactic correction for complete, explicitly stated NPC reviews."""

import json

from .schemas import ActorReview


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-JSON numeric constant")


def repair_actor_review_root_close(answer):
    """Delete only the premature root '}' after a complete checks array.

    Every review field must already exist with strict, unambiguous types. No
    values, quotes, commas, keys, defaults or missing verdicts are repaired.
    Other malformed shapes, duplicate keys and trailing documents are declined.
    """
    if not isinstance(answer, str) or len(answer) > 65536:
        return None
    start = len(answer) - len(answer.lstrip())
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    try:
        prefix, end = decoder.raw_decode(answer[start:])
        if not isinstance(prefix, dict) or set(prefix) != {"checks"}:
            return None
        position = start + end - 1
        if answer[position] != "}" or not answer[:position].rstrip().endswith("]"):
            return None
        if not answer[position + 1:].lstrip().startswith(","):
            return None
        corrected = answer[:position] + answer[position + 1:]
        review = decoder.decode(corrected)
        if set(review) != {"checks", "accepted", "delivery", "issues"}:
            return None
        if type(review["accepted"]) is not bool or review["checks"] != prefix["checks"]:
            return None
        if not isinstance(review["delivery"], dict) or not {
            "addressee_id", "expects_reply", "response_kind"
        }.issubset(review["delivery"]):
            return None
        if not set(review["delivery"]).issubset(ActorReview.model_fields["delivery"].annotation.model_fields):
            return None
        if not isinstance(review["checks"], list) or any(
            not isinstance(check, dict)
            or not {"text", "basis", "supported"}.issubset(check)
            or not set(check).issubset({"text", "basis", "supported", "reason_code"})
            or type(check["supported"]) is not bool
            for check in review["checks"]
        ):
            return None
        ActorReview.model_validate_json(corrected, strict=True)
    except (TypeError, ValueError, RecursionError):
        return None
    return corrected
