"""Conservative recovery from exact reviewer findings; never publication approval."""

import copy
import re

_LOCAL_FACT_ERRORS = frozenset(
    kind + suffix
    for kind in ("weather_state", "reading_history", "notice_receipt")
    for suffix in ("_unclaimed", "_not_in_quote")
)


def prune_local_fact_parts(speech, issues, sources, observations, question):
    """Use the same whole-part recovery for narrowly located local evidence gaps.

    Other local failures (broken source, control leak, mutation) cannot authorize
    this recovery. A surviving draft is a new candidate, never an approval.
    """
    from ..role_policy import claim_issues

    if not issues or any(issue["code"] not in _LOCAL_FACT_ERRORS for issue in issues):
        return None
    checks = []
    for part in speech.get("spoken_parts") or []:
        part_speech = {
            "text": part,
            "claims": [claim for claim in speech["claims"] if claim["text"] in part],
        }
        part_issues = claim_issues(part_speech, sources, observations, question)
        if part_issues:
            if any(issue["code"] not in _LOCAL_FACT_ERRORS for issue in part_issues):
                return None
            checks.append({"text": part, "supported": False, "reason_code": "unsupported_fact"})
    return prune_rejected_parts(speech, {"checks": checks})


def prune_rejected_parts(speech, review):
    """Drop whole spoken parts containing rejected fact spans, or decline.

    We only handle a fully aligned speech projection with no separate actions.
    No substrings are rewritten or stitched inside a sentence. The surviving
    candidate must pass ordinary local and semantic review again.
    """
    parts = speech.get("spoken_parts") or []

    def normalized(value):
        return re.sub(r"\s", "", value)

    if not parts or normalized("".join(parts)) != normalized(speech["text"]):
        return None
    rejected = [
        check["text"]
        for check in review.get("checks", [])
        if check.get("supported") is False
        and check.get("reason_code")
        in {"unsupported_fact", "invented_history", "statement_promotion", "acquisition_mismatch"}
    ]
    if not rejected or any(not text or not any(text in part for part in parts) for text in rejected):
        return None
    keep = [index for index, part in enumerate(parts) if not any(text in part for text in rejected)]
    if not keep or len(keep) == len(parts):
        return None
    retained = [parts[index] for index in keep]
    result = copy.deepcopy(speech)
    result["text"] = "\n\n".join(retained)
    result["spoken_parts"] = retained
    result["claims"] = [
        claim for claim in result["claims"] if any(claim["text"] in part for part in retained)
    ]
    performance = result.get("performance") or []
    result["performance"] = (
        [performance[index] for index in keep] if [part["text"] for part in performance] == parts else []
    )
    return result
