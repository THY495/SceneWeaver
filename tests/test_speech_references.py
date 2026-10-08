import copy

import pytest

from chatbot.npc.speech_references import normalize_own_profile_reference


def draft(source="actor", quote="渡船船长"):
    return {"text": "我是船长。", "claims": [{"text": "我是船长。", "source_id": source,
            "quote": quote, "mode": "fact"}]}


def test_exact_own_alias_changes_only_identifier_and_leaves_support_for_reviewer():
    candidate = draft()
    before = copy.deepcopy(candidate)
    repaired, count = normalize_own_profile_reference(candidate, "actor", {"profile:actor": "你是渡船船长。"})
    assert count == 1
    assert repaired["claims"][0] == {**before["claims"][0], "source_id": "profile:actor"}
    assert repaired["text"] == before["text"] and candidate == before


@pytest.mark.parametrize("source,quote,sources", [
    ("other-actor", "渡船船长", {"profile:actor": "你是渡船船长。"}),
    ("actor", "船况一切正常", {"profile:actor": "你是渡船船长。"}),
    ("actor", "", {"profile:actor": "你是渡船船长。"}),
    ("actor", "渡船船长", {}),
    ("actor", "渡船船长", {"profile:actor": "你是渡船船长。", "actor": "不同的事件"}),
])
def test_no_guessing_foreign_identity_wrong_quote_missing_profile_or_collision(source, quote, sources):
    candidate = draft(source, quote)
    repaired, count = normalize_own_profile_reference(candidate, "actor", sources)
    assert count == 0 and repaired is candidate


def test_unmatched_candidate_text_stays_invalid():
    candidate = draft()
    candidate["claims"][0]["text"] = "我检查过船了。"
    assert normalize_own_profile_reference(candidate, "actor", {"profile:actor": "渡船船长"}) == (candidate, 0)
