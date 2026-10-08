import importlib.util
import json
from pathlib import Path

from chatbot.npc.review import REVIEW_SYSTEM
from chatbot.providers import StructuredOutputError


def test_execution_target_cases_bind_live_assignment_without_sending_grades():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = json.loads((root / "evals/cases/reviewer-ferry-execution-target-v1.json").read_text("utf-8"))
    for case in module.materialize_cases(suite):
        messages = module.candidate_messages(case)
        requirement = json.loads(messages[-1]["content"])["authorized_performance_requirement"]
        assert requirement == case.get("director_assignment")
        altered = {**case, "expected_accept": not case["expected_accept"], "rationale": "SENTINEL"}
        assert module.candidate_messages(altered) == messages


def test_goal_rejection_cannot_hide_a_spurious_fact_rejection_in_summary():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = {"expected_accept": False, "observed_accept": False, "expected_unsupported_check_count": 0,
           "review": {"checks": [{"supported": False}]}}
    summary = module.summarize([row])
    assert summary["matched"] == 1
    assert summary["fact_finding_cases"] == 1 and summary["fact_finding_matched"] == 0


def test_assignment_calibration_uses_live_builder_and_cannot_send_labels():
    from chatbot.npc.assignment import assignment_messages

    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = json.loads((root / "evals/cases/assignment-archive-history-length-v1.json").read_text("utf-8"))
    plan = module.materialize_cases(suite)
    assert len(plan) == 8
    for case in plan:
        messages = module.candidate_messages(case)
        data = json.loads(messages[1]["content"])
        assert set(data) == {"performer_id", "authorized_state", "assignment"}
        assert messages == assignment_messages(messages[1]["content"])
        altered = {**case, "expected_accept": not case["expected_accept"], "rationale": "SENTINEL"}
        assert module.candidate_messages(altered) == messages
        assert "SENTINEL" not in str(messages)


def test_calibration_uses_live_messages_without_expected_labels():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = json.loads((root / "evals/cases/reviewer-calibration-v1.json").read_text("utf-8"))
    assert len(suite["cases"]) == 12
    assert sum(c["expected_accept"] for c in suite["cases"]) == 6
    assert "reason_code=mixed_recipient" in REVIEW_SYSTEM
    for case in suite["cases"]:
        messages = module.candidate_messages(case)
        assert messages[0]["content"] == REVIEW_SYSTEM
        altered = {
            **case,
            "expected_accept": not case["expected_accept"],
            "case_id": "SENTINEL_ID",
            "rationale": "SENTINEL_LABEL",
        }
        assert module.candidate_messages(altered) == messages
        assert "SENTINEL" not in str(messages)


def test_complex_calibration_freezes_paired_plan_without_label_leakage():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = json.loads((root / "evals/cases/reviewer-complex-calibration-v1.json").read_text("utf-8"))
    plan = module.materialize_cases(suite)
    assert len(plan) == 24
    assert len({case["case_id"] for case in plan}) == 24
    assert len({case["pair_id"] for case in plan}) == 12
    assert all(sum(other["pair_id"] == case["pair_id"] for other in plan) == 2 for case in plan)
    assert sum(case["expected_accept"] for case in plan) == 18
    for case in plan:
        messages = module.candidate_messages(case)
        altered = {
            **case,
            "expected_accept": not case["expected_accept"],
            "expected_reason_codes": ["SENTINEL_REASON"],
            "pair_id": "SENTINEL_PAIR",
            "factor": "SENTINEL_FACTOR",
            "variant": "SENTINEL_VARIANT",
            "rationale": "SENTINEL_RATIONALE",
        }
        assert module.candidate_messages(altered) == messages
        assert "SENTINEL" not in str(messages)


def test_calibration_plan_and_summary_are_bounded_and_explicit():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = json.loads((root / "evals/cases/reviewer-complex-calibration-v1.json").read_text("utf-8"))
    plan = module.materialize_cases(
        suite,
        ["length-concise-supported", "length-extended-supported"],
        repeats=3,
    )
    assert [case["repeat"] for case in plan] == [1, 2, 3, 1, 2, 3]
    rows = [
        {
            "case_id": "accepted",
            "pair_id": "pair",
            "factor": "length",
            "expected_accept": True,
            "observed_accept": True,
            "marks": ["semantic_accepted"],
        },
        {
            "case_id": "false-reject",
            "pair_id": "pair",
            "factor": "length",
            "expected_accept": True,
            "expected_reason_codes": ["unsupported_fact"],
            "observed_accept": False,
            "marks": ["semantic_rejected", "reason_other"],
        },
        {"expected_accept": False, "error_type": "ProviderError"},
    ]
    rows[0].update(
        expected_expects_reply=False,
        expected_addressee_id="peer",
        expected_requires_player_action=False,
        review={"delivery": {"expects_reply": True, "addressee_id": "peer", "requires_player_action": False}},
    )
    rows[1].update(
        expected_expects_reply=True,
        expected_addressee_id="player",
        expected_requires_player_action=True,
        review={"delivery": {"expects_reply": True, "addressee_id": "peer", "requires_player_action": None}},
    )
    summary = module.summarize(rows)
    assert summary == {
        "planned": 3,
        "completed": 2,
        "errors": 1,
        "matched": 1,
        "false_accept": 0,
        "false_reject": 1,
        "error_types": {"ProviderError": 1},
        "schema_error_types": {},
        "delivery_cases": 2,
        "delivery_matched": 1,
        "addressee_cases": 2,
        "addressee_matched": 1,
        "action_prerequisite_cases": 2,
        "action_prerequisite_matched": 1,
        "observed_rejection_reasons": {"other": 1},
        "reason_cases": 1,
        "fact_finding_cases": 0,
        "fact_finding_matched": 0,
        "reason_covered": 0,
        "reason_mismatches": [
            {
                "case_id": "false-reject",
                "expected": ["unsupported_fact"],
                "observed": ["other"],
            }
        ],
        "pairs": {
            "pair": {
                "factor": "length",
                "completed": 2,
                "matched": 1,
                "observed_accept": 1,
            }
        },
    }

    clarified = json.loads((root / "evals/cases/reviewer-complex-calibration-v2.json").read_text("utf-8"))
    clarified_plan = module.materialize_cases(clarified, repeats=3)
    assert len(clarified_plan) == 6
    assert {case["expected_accept"] for case in clarified_plan} == {True, False}
    assert all("玩家" not in case["speech"]["text"] for case in clarified_plan)

    isolated = json.loads((root / "evals/cases/reviewer-complex-calibration-v3.json").read_text("utf-8"))
    isolated_plan = module.materialize_cases(isolated, repeats=3)
    assert len(isolated_plan) == 6
    assert all(
        "记录" not in case["speech"]["text"] and "钥匙" not in case["speech"]["text"]
        for case in isolated_plan
    )


def test_calibration_reports_schema_failure_without_model_text():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    exc = StructuredOutputError(
        '{"private":"do not report"}',
        [
            {"path": "checks.0.reason_code", "type": "literal_error"},
            {"path": "do not report", "type": "literal_error"},
        ],
    )
    safe = module.safe_schema_errors(exc)
    assert safe == [
        {"path": "checks.0.reason_code", "type": "literal_error"},
        {"path": "", "type": "literal_error"},
    ]
    assert "do not report" not in str(safe)
    assert module.summarize([{"error_type": "StructuredOutputError", "schema_errors": safe}])[
        "schema_error_types"
    ] == {"literal_error": 2}


def test_ferry_current_state_pairs_keep_expected_labels_out_of_live_review():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = json.loads((root / "evals/cases/reviewer-ferry-current-state-v1.json").read_text("utf-8"))
    plan = module.materialize_cases(suite)
    assert len(plan) == 6
    assert sum(case["expected_accept"] for case in plan) == 3
    assert {case["pair_id"] for case in plan} == {
        "weather-current-status",
        "notification-state",
        "compound-current-status",
    }
    for case in plan:
        messages = module.candidate_messages(case)
        altered = {**case, "expected_accept": not case["expected_accept"], "rationale": "SENTINEL"}
        assert module.candidate_messages(altered) == messages
        assert "SENTINEL" not in str(messages)


def test_ferry_plural_recipient_cases_are_paired_without_label_leakage():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = json.loads((root / "evals/cases/reviewer-ferry-plural-recipient-v1.json").read_text("utf-8"))
    plan = module.materialize_cases(suite)
    assert len(plan) == 3
    assert [case["expected_accept"] for case in plan] == [False, True, True]
    assert [case["speech"]["text"].count("？") for case in plan] == [1, 1, 0]
    for case in plan:
        messages = module.candidate_messages(case)
        assert messages[0]["content"] == REVIEW_SYSTEM
        assert case["player_request"] in str(messages)
        altered = {
            **case,
            "expected_accept": not case["expected_accept"],
            "expected_reason_codes": ["SENTINEL_REASON"],
            "rationale": "SENTINEL_RATIONALE",
        }
        assert module.candidate_messages(altered) == messages
        assert "SENTINEL" not in str(messages)


def test_archive_recipient_holdout_uses_runtime_ids_without_label_leakage():
    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("calibrate", root / "scripts/calibrate_reviewer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    suite = json.loads((root / "evals/cases/reviewer-archive-recipient-holdout-v1.json").read_text("utf-8"))
    plan = module.materialize_cases(suite)
    assert len(plan) == 8
    assert sum(case["expected_accept"] for case in plan) == 4
    for case in plan:
        messages = module.candidate_messages(case)
        assert messages[0]["content"] == REVIEW_SYSTEM
        altered = {**case, "expected_accept": not case["expected_accept"], "rationale": "SENTINEL"}
        assert module.candidate_messages(altered) == messages
        assert "SENTINEL" not in str(messages)
        assert case["actor_id"] in {actor["id"] for actor in case["cast"]}
        packet = json.loads(messages[1]["content"])
        assert (
            packet["recent_observed_utterances"][0]["addressees"]
            == case["info"]["observations"][0]["addressees"]
        )
