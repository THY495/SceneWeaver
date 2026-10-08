import json

import pytest
from pydantic import ValidationError

from chatbot.npc.assignment import AssignmentReview, validate_assignment_evidence
from chatbot.npc.assignment_reference import (
    ReferenceReview,
    evidence_catalog,
    normalize_reference,
    reference_messages,
)


def review(reference, premise="world_fact", extent="full", supported=True):
    return {
        "accepted": True,
        "checks": [
            {
                "text": "任务",
                "basis": "资料",
                "supported": supported,
                "premise_kind": premise,
                "support_extent": extent,
                "source_ref": reference,
            }
        ],
    }


def test_catalog_long_record_is_complete_and_each_citation_is_bounded_original_slice():
    info = {"profile": {"core": "这是长档案" * 900}}
    original = json.dumps(info["profile"], ensure_ascii=False)
    records = evidence_catalog(info)
    assert len(records) > 2
    assert records[0]["slice_start"] == 0 and records[-1]["slice_end"] == len(original)
    for index, record in enumerate(records):
        assert record["text"] == original[record["slice_start"] : record["slice_end"]]
        assert len(record["text"]) <= 1500
        if index:
            assert record["slice_start"] <= records[index - 1]["slice_end"]


@pytest.mark.parametrize("reference", ["observed-0:0", "unknown:0"])
def test_existing_utterance_or_unknown_reference_cannot_establish_world_fact(reference):
    info = {"observations": [{"kind": "speech", "text": "有人宣称昨夜收到了通知"}]}
    value = normalize_reference(review(reference), evidence_catalog(info))
    guarded = validate_assignment_evidence(AssignmentReview.model_validate(value), info)
    assert guarded.checks[0].supported is False


def test_statement_reference_preserves_assertion_as_statement_and_never_fixes_model_rejection():
    info = {"observations": [{"kind": "speech", "text": "有人宣称昨夜收到了通知"}]}
    source = review("observed-0:0", premise="reported_statement", supported=False)
    source.update(accepted=False, issues=["仍不支持任务"])
    value = normalize_reference(source, evidence_catalog(info))
    assert value["accepted"] is False and value["checks"][0]["supported"] is False
    assert value["issues"] == source["issues"]
    assert value["checks"][0]["source_kind"] == "utterance"
    assert value["checks"][0]["source_quote"] == info["observations"][0]["text"]


@pytest.mark.parametrize(
    "source",
    [
        review("profile:0", premise="expression", extent="not_needed"),
        review(None),
        review(None, extent="partial", supported=True),
    ],
)
def test_schema_rejects_inconsistent_reference_fields(source):
    with pytest.raises(ValidationError):
        ReferenceReview.model_validate(source)


def test_host_claim_excluded_and_confirmed_event_is_classified_by_actual_event_type():
    records = evidence_catalog(
        {
            "observations": [
                {"kind": "host", "text": "天气已变"},
                {"kind": "speech", "text": "天气已变", "detail": {"basis": "confirmed"}},
                {"kind": "host", "text": "两人已到场", "detail": {"basis": "confirmed"}},
            ]
        }
    )
    assert [(r["id"], r["kind"]) for r in records] == [
        ("observed-1:0", "utterance"),
        ("observed-2:0", "confirmed_event"),
    ]


def test_recovery_request_keeps_authorized_state_and_indexes_without_prior_verdict():
    data = {"assignment": {"objective": "回应他人的话"}, "authorized_state": {"profile": {"core": "档案"}}}
    messages = [
        {"role": "system", "content": "每条必填premise_kind、support_extent、source_kind、source_quote。"},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]
    result = reference_messages(messages, evidence_catalog(data["authorized_state"]), ["quote_not_found"])
    request = json.loads(result[1]["content"])
    assert all(request[key] == value for key, value in data.items())
    assert request["evidence_catalog"][0]["location"] == "/authorized_state/profile"
    assert "text" not in request["evidence_catalog"][0]
    assert request["runtime_evidence_errors"] == ["quote_not_found"]
    assert len(result) == 2 and "review_to_recheck" not in request
