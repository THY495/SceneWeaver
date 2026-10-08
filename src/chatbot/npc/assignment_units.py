"""Quoted-text aware units for bounded assignment-review experiments.

Punctuation describes text boundaries, never truth or permissions. Oversized
inputs explicitly decline coverage rather than silently dropping any text.
"""


def assignment_units(assignment, *, limit=12):
    units = []
    fields = [("objective", assignment["objective"])] + [
        (f"constraint-{index}", text) for index, text in enumerate(assignment.get("delivery_constraints", []))
    ]
    pairs = {
        "“": "”",
        "「": "」",
        "『": "』",
        "‘": "’",
        "（": "）",
        "(": ")",
        "[": "]",
        "【": "】",
        "《": "》",
    }
    for name, text in fields:
        stack = []
        begin = 0
        escaped = False

        def add(end):
            raw = text[begin:end]
            value = raw.strip()
            if value:
                offset = begin + len(raw) - len(raw.lstrip())
                units.append(
                    {
                        "id": f"{name}:{len(units)}",
                        "field": name,
                        "start": offset,
                        "end": offset + len(value),
                        "text": value,
                    }
                )

        for index, char in enumerate(text):
            if escaped:
                escaped = False
                continue
            if char == "\\" and stack:
                escaped = True
                continue
            if stack and stack[-1] in {'"', "'", "”", "’", "」", "』"}:
                if char == stack[-1]:
                    stack.pop()
                continue
            if stack and char == stack[-1]:
                stack.pop()
            elif char in {'"', "'"}:
                if (
                    char == "'"
                    and index
                    and index + 1 < len(text)
                    and text[index - 1].isalnum()
                    and text[index + 1].isalnum()
                ):
                    continue
                if not stack:
                    stack.append(char)
            elif char in pairs:
                stack.append(pairs[char])
            elif not stack and char in "，,；;。！？\n":
                add(index)
                begin = index + 1
        add(len(text))
    return units if len(units) <= limit else None


def validate_assignment_coverage(review, units):
    """Every unit has exactly one full-span check; no merging or omitted units."""
    expected = {unit["id"]: unit["text"] for unit in units}
    counts = {key: 0 for key in expected}
    valid = True
    for check in review.checks:
        if check.unit_id not in expected or check.text != expected[check.unit_id]:
            valid = False
            check.supported = False
        else:
            counts[check.unit_id] += 1
    valid = valid and all(count == 1 for count in counts.values())
    if not valid:
        review.accepted = False
        review.issues = list(
            dict.fromkeys([*review.issues, "任务原文片段未完整逐项核验，或检查跨片段合并。"])
        )[:6]
    return review
