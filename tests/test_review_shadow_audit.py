import importlib.util
import json
from pathlib import Path

import pytest


def script():
    spec = importlib.util.spec_from_file_location(
        "review_shadow", Path(__file__).parents[1] / "scripts/audit_review_shadow.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def suite(path, label, text="我觉得我们早就约定好了。"):
    path.write_text(
        json.dumps(
            {
                "defaults": {"intent": {"act": "speak"}, "info": {"private": "NOT_EXPORTED"}},
                "cases": [
                    {"case_id": "candidate", "expected_accept": label, "speech": {"text": text, "claims": []}}
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_labels_never_affect_proposal_and_private_content_is_not_exported(tmp_path):
    module = script()
    first = suite(tmp_path / "one.json", False)
    rows, manifests = module.load_suites([first])
    before = rows[0]["proposed_low_risk"]
    first = suite(first, True)
    changed, _ = module.load_suites([first])
    assert changed[0]["proposed_low_risk"] == before
    assert changed[0]["input_hash"] == rows[0]["input_hash"]
    result = module.summarize(rows, manifests)
    assert "NOT_EXPORTED" not in json.dumps(result)
    assert "我们早就约定" not in json.dumps(result)
    assert (
        result["hypotheses"]["stance_prefix"]["distinct_nonconflicting_inputs"]["expected_reject_in_low_risk"]
        == 1
    )


def test_legacy_conflicting_labels_are_not_hidden_or_used_in_pooled_metrics(tmp_path):
    module = script()
    rows, manifests = module.load_suites(
        [suite(tmp_path / "old.json", True), suite(tmp_path / "new.json", False)]
    )
    result = module.summarize(rows, manifests)
    assert result["case_rows"] == 2 and result["distinct_inputs"] == 1
    assert len(result["conflicting_labels"]) == 1
    assert result["hypotheses"]["empty_claims"]["all_nonconflicting_cases"]["cases"] == 0


def test_duplicate_versions_do_not_inflate_distinct_input_count(tmp_path):
    module = script()
    rows, manifests = module.load_suites(
        [suite(tmp_path / "a.json", False), suite(tmp_path / "b.json", False)]
    )
    result = module.summarize(rows, manifests)["hypotheses"]["empty_claims"]
    assert result["all_nonconflicting_cases"]["cases"] == 2
    assert result["distinct_nonconflicting_inputs"]["cases"] == 1


def test_unlabelled_cases_fail_instead_of_becoming_safe(tmp_path):
    module = script()
    with pytest.raises(ValueError, match="Missing independent label"):
        module.load_suites([suite(tmp_path / "missing.json", None)])
