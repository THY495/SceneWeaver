import pytest

from chatbot.directing import DirectorPlan
from chatbot.npc.dialogue_capacity import reply_capacity, unavailable_npc_request


def plan(*, handoff=True):
    return DirectorPlan(
        beat_id="b",
        reason="交流",
        handoff=handoff,
        jobs=[
            {"id": "one", "actor": "a", "objective": "表达意见"},
            {"id": "two", "actor": "b", "objective": "回应甲"},
            {"id": "three", "actor": "c", "objective": "说明未知"},
        ],
    )


@pytest.mark.parametrize("index,target,unavailable", [(0, "b", False), (1, "a", True), (2, "b", True)])
def test_question_needs_an_actual_remaining_reply_slot(index, target, unavailable):
    capacity = reply_capacity(plan(), index, {"a", "b", "c"}, 3)
    assert (
        unavailable_npc_request({"expects_reply": True, "addressee_id": target}, capacity, "player")
        is unavailable
    )
    assert not unavailable_npc_request({"expects_reply": False, "addressee_id": target}, capacity, "player")
    assert not unavailable_npc_request({"expects_reply": True, "addressee_id": "player"}, capacity, "player")


def test_short_plan_can_reserve_future_work_without_widening_visibility():
    capacity = reply_capacity(plan(handoff=False), 2, {"a", "b", "c"}, 4)
    assert capacity["can_schedule_more"] is True
    assert not unavailable_npc_request({"expects_reply": True, "addressee_id": "b"}, capacity, "player")
    exhausted = reply_capacity(plan(handoff=False), 2, {"a", "b", "c"}, 3)
    assert unavailable_npc_request({"expects_reply": True, "addressee_id": "b"}, exhausted, "player")
    assert reply_capacity(plan(), 0, {"a", "b"}, 3)["scheduled_reply_actor_ids"] == ["b"]
    assert reply_capacity(plan(), 0, {"a", "b"}, None) is None
