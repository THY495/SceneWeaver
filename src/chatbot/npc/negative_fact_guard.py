"""Narrow evidence sentinels for known negative world/reading misclassifications.

They do not decide general entailment. A claimed negative still goes through
the existing provenance and semantic review after this necessary check.
"""

import re

_WEATHER = re.compile(
    r"(?:风势|雨势|风|雨|雪|雾)(?:目前|现在|眼下|仍然|依然|仍|还)?"
    r"(?:还没|尚未|没有|未|没)(?:真正|真|完全)?(?:压下来|增大|变大|停|散|起|下)"
)
_READING = re.compile(r"(?:从未|没有|尚未|还没|没|未)(?:看过|读过|翻过|看清(?:楚)?)")
_READING_OBJECT = re.compile(r"便条|原文|逐字|字迹|登记册|公告|档案|记录|书页|文字")
_IRREALIS = re.compile(
    r"(?:如果|假如|若是|要是|倘若|若|但愿|希望|可能|也许|会不会|是否)[^，,。！？!?；;\n]{0,12}$"
)


def negative_fact_kinds(text):
    kinds = set()
    for clause in re.split(r"[。！？!?；;，,\n]", text):
        for kind, pattern in (("weather_state", _WEATHER), ("reading_history", _READING)):
            for match in pattern.finditer(clause):
                if _IRREALIS.search(clause[: match.start()]):
                    continue
                if kind == "reading_history" and not _READING_OBJECT.search(clause):
                    continue
                kinds.add(kind)
    return kinds


def negative_fact_issues(speech):
    """Every recognized assertion needs a claim, whose quote repeats its kind.

    The quote must also exist in the authorized source map (checked by the
    caller). Matching a kind alone never proves the speaker, object or scope.
    """
    issues = []
    claimed = set().union(*(negative_fact_kinds(claim["text"]) for claim in speech["claims"]))
    for kind in sorted(negative_fact_kinds(speech["text"]) - claimed):
        issues.append({"code": kind + "_unclaimed"})
    for index, claim in enumerate(speech["claims"]):
        for kind in sorted(negative_fact_kinds(claim["text"]) - negative_fact_kinds(claim["quote"])):
            issues.append({"claim_index": index, "code": kind + "_not_in_quote"})
    return issues
