import copy

from chatbot.npc.speech_pruning import prune_local_fact_parts, prune_rejected_parts
from chatbot.role_policy import claim_issues


def sample():
    parts = ["公告栏在左手边。", "你看过以后，愿意的话回来聊聊。"]
    return {
        "text": "\n\n".join(parts),
        "spoken_parts": parts,
        "claims": [{"text": parts[0], "quote": "值班员", "source_id": "profile"}],
        "performance": [{"text": part, "expression": "neutral"} for part in parts],
    }


def rejection(text="公告栏在左手边。", reason="unsupported_fact"):
    return {"accepted": False, "checks": [{"text": text, "supported": False, "reason_code": reason}]}


def test_delete_only_whole_rejected_part_without_mutating_original():
    speech = sample()
    original = copy.deepcopy(speech)
    candidate = prune_rejected_parts(speech, rejection())
    assert candidate["text"] == speech["spoken_parts"][1]
    assert candidate["claims"] == []
    assert candidate["performance"] == speech["performance"][1:]
    assert speech == original


def test_decline_when_all_parts_removed_or_finding_is_not_exactly_located():
    speech = sample()
    assert prune_rejected_parts(speech, rejection("不存在的句子")) is None
    review = rejection()
    review["checks"].append(
        {"text": speech["spoken_parts"][1], "supported": False, "reason_code": "unsupported_fact"}
    )
    assert prune_rejected_parts(speech, review) is None
    assert prune_rejected_parts(speech, rejection(reason="mixed_recipient")) is None


def test_separate_actions_and_cross_part_spans_are_not_silently_edited():
    speech = sample()
    speech["text"] += "（点头）"
    assert prune_rejected_parts(speech, rejection()) is None
    speech = sample()
    assert prune_rejected_parts(speech, rejection(speech["text"])) is None


def test_local_history_gap_deletes_whole_part_preserving_answer_and_source():
    parts = ["便条我没看过逐字内容。", "公告写的是遇强风停航。", "便条上具体写了什么，我现在说不上来。"]
    speech = {
        "text": "".join(parts),
        "spoken_parts": parts,
        "claims": [{"text": parts[1], "source_id": "notice", "quote": "遇强风停航", "mode": "reported"}],
        "performance": [{"text": part, "expression": "neutral"} for part in parts],
    }
    original = copy.deepcopy(speech)
    sources = {"notice": "遇强风停航"}
    issues = claim_issues(speech, sources, [], "便条写什么")
    candidate = prune_local_fact_parts(speech, issues, sources, [], "便条写什么")
    assert candidate["spoken_parts"] == parts[1:]
    assert candidate["claims"] == speech["claims"]
    assert candidate["performance"] == speech["performance"][1:]
    assert not claim_issues(candidate, sources, [], "便条写什么")
    assert speech == original


def test_local_pruning_declines_unrelated_failures_actions_and_empty_result():
    speech = sample()
    issues = [{"code": "weather_state_unclaimed"}, {"code": "source_unavailable"}]
    assert prune_local_fact_parts(speech, issues, {}, [], "") is None
    speech = {"text": "风还没增大。", "spoken_parts": ["风还没增大。"], "claims": []}
    issues = claim_issues(speech, {}, [], "")
    assert prune_local_fact_parts(speech, issues, {}, [], "") is None
    speech["spoken_parts"].append("我不知道。")
    speech["text"] += "我不知道。（点头）"
    assert prune_local_fact_parts(speech, issues, {}, [], "") is None
