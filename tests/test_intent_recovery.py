import json

from chatbot.npc.intent_recovery import (
    execution_intent,
    purpose_recovery_messages,
    unsupported_candidate_checks,
)


def test_recovery_only_uses_fact_findings_in_actual_candidate():
    checks = [
        {"text": "公告栏在左手边。", "supported": False, "reason_code": "unsupported_fact"},
        {"text": "提供公告栏位置", "supported": False, "reason_code": "unsupported_fact"},
        {"text": "公告栏", "supported": False, "reason_code": "mixed_recipient"},
        {"text": "公告栏", "supported": True, "reason_code": "none"},
    ]
    assert unsupported_candidate_checks({"checks": checks}, {"text": "公告栏在左手边。"}) == checks[:1]


def test_recovery_messages_preserve_original_request_and_routing_as_data():
    info = {"profile": {"name": "沈舟"}, "observations": []}
    intent = {
        "act": "speak",
        "purpose": "提醒公告栏位置",
        "addressee_id": "player",
        "reply_to": "question",
        "remaining_request": "询问天气",
    }
    messages = purpose_recovery_messages(info, intent, "公告栏在哪里？", [], [])
    payload = json.loads(messages[-1]["content"])
    assert payload["original_intent"] == intent
    assert payload["current_player_request"] == "公告栏在哪里？"
    assert payload["authorized_state"] == info
    assert "rejected_candidate" not in payload


def test_recovered_generation_and_review_do_not_receive_discarded_rationale():
    intent = {
        "purpose": "回应玩家自己去查看的决定",
        "trigger_reason": "可提供具体方位",
        "addressee_id": "player",
        "reply_to": "pending",
        "act": "speak",
    }
    assert execution_intent(intent) == intent
    effective = execution_intent(intent, recovered=True)
    assert "trigger_reason" not in effective
    assert effective["addressee_id"] == "player" and effective["reply_to"] == "pending"
    assert intent["trigger_reason"] == "可提供具体方位"
