import json

import pytest

from chatbot.npc.assignment import AssignmentReview, assignment_messages
from chatbot.npc.assignment_units import assignment_units, validate_assignment_coverage


@pytest.mark.parametrize(
    "text,parts",
    [
        ("  面向旅人，说明未知；不替玩家决定。", ["面向旅人", "说明未知", "不替玩家决定"]),
        ("转述“我不知道，尚未核对”；不确认原话。", ["转述“我不知道，尚未核对”", "不确认原话"]),
        (
            "转述“我不知道（核对，未完）；这是假设”；说明来源。",
            ["转述“我不知道（核对，未完）；这是假设”", "说明来源"],
        ),
        ('Quote "I do not know, yet"; ask.', ['Quote "I do not know, yet"', "ask."]),
        ("Don't presume; ask.", ["Don't presume", "ask."]),
        ("讨论《核对，还是暂缓》；承认未知", ["讨论《核对，还是暂缓》", "承认未知"]),
        ("说明（来源，范围）；然后提问", ["说明（来源，范围）", "然后提问"]),
    ],
)
def test_units_preserve_quoted_punctuation_and_exact_offsets(text, parts):
    units = assignment_units({"objective": text, "delivery_constraints": []})
    assert [unit["text"] for unit in units] == parts
    assert all(text[unit["start"] : unit["end"]] == unit["text"] for unit in units)


def test_overflow_declines_coverage_instead_of_truncating_units():
    assert assignment_units({"objective": "；".join(str(n) for n in range(13))}) is None
    with pytest.raises(ValueError, match="capacity"):
        assignment_messages(
            json.dumps({"assignment": {"objective": "；".join(str(n) for n in range(13))}}),
            clause_coverage=True,
        )


@pytest.mark.parametrize("defect", ["none", "missing", "duplicate", "merged", "rewritten", "unknown_id"])
def test_coverage_rejects_missing_or_cross_unit_checks(defect):
    units = assignment_units({"objective": "尚未经核对，不知道是否准确"})
    checks = [
        {"text": unit["text"], "unit_id": unit["id"], "basis": "检查依据", "supported": True}
        for unit in units
    ]
    if defect == "missing":
        checks.pop()
    elif defect == "duplicate":
        checks.append(dict(checks[0]))
    elif defect == "merged":
        checks[0]["text"] += checks.pop()["text"]
    elif defect == "rewritten":
        checks[0]["text"] = "从未核对"
    elif defect == "unknown_id":
        checks[0]["unit_id"] = "missing"
    review = AssignmentReview.model_validate({"checks": checks, "accepted": True, "issues": []})
    assert validate_assignment_coverage(review, units).accepted is (defect == "none")


def test_coverage_payload_is_derived_only_from_assignment_and_retains_full_context():
    data = {
        "assignment": {"objective": "面向乙，说明尚未核对，不知道是否准确", "delivery_constraints": []},
        "authorized_state": {"observations": [{"text": "玩家说过，属于背景"}]},
    }
    messages = assignment_messages(json.dumps(data, ensure_ascii=False), clause_coverage=True)
    payload = json.loads(messages[1]["content"])
    assert payload["authorized_state"] == data["authorized_state"]
    assert payload["assignment"] == data["assignment"]
    assert all("背景" not in unit["text"] for unit in payload["assignment_units"])
