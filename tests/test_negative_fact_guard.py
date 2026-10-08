import pytest

from chatbot.npc.negative_fact_guard import negative_fact_kinds
from chatbot.role_policy import claim_issues


@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        ("雷声才过去没多久，风还没真压下来。", {"weather_state"}),
        ("雨还没停，先等等。", {"weather_state"}),
        ("便条我看到了，但没看清楚逐字写的是什么。", {"reading_history"}),
        ("登记册原文我没读过。", {"reading_history"}),
        ("风势我不知道。我现在不知道便条文字。", set()),
        ("我不想冒险；我没看清你的意思。", set()),
        ("如果风还没增大，我打算等等。", set()),
        ("若是没看清楚便条文字，可以问问。", set()),
        ("希望雨还没停；现在雨还没停。", {"weather_state"}),
    ],
)
def test_fact_sentinels_do_not_turn_unknown_hypotheses_or_attitude_into_history(text, kinds):
    assert negative_fact_kinds(text) == kinds


@pytest.mark.parametrize(
    ("text", "kind", "insufficient"),
    [
        ("风还没真压下来。", "weather_state", "一阵雷声从远处传来。"),
        ("没看清楚便条逐字内容。", "reading_history", "观察到便条由旅人持有。"),
    ],
)
def test_source_presence_alone_cannot_ground_an_assertion(text, kind, insufficient):
    assert claim_issues({"text": text, "claims": []}, {"s": insufficient}, [], "") == [
        {"code": kind + "_unclaimed"}
    ]
    claim = {"text": text, "source_id": "s", "quote": insufficient, "mode": "fact"}
    assert claim_issues({"text": text, "claims": [claim]}, {"s": insufficient}, [], "") == [
        {"claim_index": 0, "code": kind + "_not_in_quote"}
    ]
    claim["quote"] = text
    assert claim_issues({"text": text, "claims": [claim]}, {"s": text}, [], "") == []
    claim["quote"] = "如果" + text
    assert claim_issues({"text": text, "claims": [claim]}, {"s": claim["quote"]}, [], "") == [
        {"claim_index": 0, "code": kind + "_not_in_quote"}
    ]


def test_same_negative_kind_is_only_a_necessary_check_not_object_entailment():
    claim = {"text": "便条原文我没读过。", "source_id": "s", "quote": "登记册原文我没读过。", "mode": "fact"}
    # The later semantic reviewer must reject this different object. The local
    # sentinel does not claim to prove arbitrary subject/object entailment.
    assert claim_issues({"text": claim["text"], "claims": [claim]}, {"s": claim["quote"]}, [], "") == []
