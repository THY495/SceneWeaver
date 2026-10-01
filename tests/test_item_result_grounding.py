from chatbot.provenance import source_class
from chatbot.role_policy import invalid_statement_promotions, review_grounding


def test_observed_result_is_grounding_but_speech_and_unknown_actions_are_not():
    event = {
        "id": "failure",
        "kind": "action",
        "text": "容器已上锁，未打开。",
        "detail": {"item_action_result": {"status": "rejected", "code": "locked"}},
    }
    claim = {"text": "刚才没打开。", "source_id": "failure", "quote": "未打开", "mode": "fact"}
    speech = {"claims": [claim]}
    assert source_class(event) == "item_action_result"
    assert invalid_statement_promotions(speech, [event]) == []
    for kind in ["speech", "host"]:
        assert invalid_statement_promotions(speech, [{**event, "kind": kind}]) == [claim]
    assert invalid_statement_promotions(speech, [{**event, "detail": {}}]) == [claim]
    info = {"profile": {}, "observations": [event]}
    grounding = review_grounding(info, [], speech, {"failure": event["text"]})
    assert grounding["primary_grounding"]["item_action_results"][0]["result"]["status"] == "rejected"
    # Another actor without the private record gains no evidence from this helper.
    assert (
        review_grounding({"profile": {}, "observations": []}, [], {"claims": []}, {})["primary_grounding"][
            "item_action_results"
        ]
        == []
    )
