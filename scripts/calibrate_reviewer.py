"""Explicitly paid synthetic calibration using the same reviewer messages as live NPCs."""

import argparse
import asyncio
import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from chatbot.config import Settings
from chatbot.npc.assignment import AssignmentReview, assignment_messages, validate_assignment_evidence
from chatbot.npc.assignment_units import assignment_units, validate_assignment_coverage
from chatbot.npc.response_review import BoundActorReview, enforce_binding, response_source
from chatbot.npc.review import review_messages
from chatbot.observability.timing import TimingTrace, annotate, measure
from chatbot.providers import BudgetExceeded, Provider, StructuredOutputError, TransientProviderError
from chatbot.role_policy import semantic_review_marks
from chatbot.runtime import Runtime
from chatbot.schemas import ActorReview
from chatbot.storage import Database, Task, Usage, now, uid


def candidate_messages(case, *, clause_coverage=False, explanation="full", response_binding=False):
    if case.get("target") == "assignment":
        if explanation != "full" or response_binding:
            raise ValueError("Speech explanation mode cannot change assignment review")
        return assignment_messages(
            json.dumps(
                {
                    "performer_id": case.get("actor_id", "npc"),
                    "authorized_state": case["info"],
                    "assignment": case["assignment"],
                    **(
                        {"dialogue_capacity": case["dialogue_capacity"]}
                        if "dialogue_capacity" in case
                        else {}
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            clause_coverage=clause_coverage,
        )
    return review_messages(
        case["info"],
        [],
        case["speech"],
        case["sources"],
        actor_id=case.get("actor_id", "npc"),
        documents=[],
        intent=case["intent"],
        player_request=case.get("player_request", ""),
        cast=case["cast"],
        director_assignment=case.get("director_assignment"),
        explanation=explanation,
        response_binding=response_binding,
    )


def materialize_cases(suite, selected_ids=None, repeats=1):
    """Freeze the complete call plan before any paid request is made."""
    defaults = suite.get("defaults", {})
    target = suite.get("target", "speech")
    if target not in {"speech", "assignment"}:
        raise ValueError("Unknown calibration target")
    cases = [{**copy.deepcopy(defaults), **copy.deepcopy(case)} for case in suite["cases"]]
    for case in cases:
        case["target"] = target
    ids = [case.get("case_id") for case in cases]
    if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("Calibration case IDs must be present and unique")
    if selected_ids:
        unknown = set(selected_ids) - set(ids)
        if unknown:
            raise ValueError("Unknown calibration case")
        selected = set(selected_ids)
        cases = [case for case in cases if case["case_id"] in selected]
    if repeats < 1 or repeats > 3 or len(cases) * repeats > 24:
        raise ValueError("Calibration is limited to 24 planned calls and 1-3 repeats")
    required = (
        {"info", "assignment", "expected_accept"}
        if target == "assignment"
        else {"info", "speech", "sources", "intent", "cast", "expected_accept"}
    )
    if any(required - set(case) for case in cases):
        raise ValueError("Calibration case is incomplete")
    plan = []
    for case in cases:
        for repeat in range(1, repeats + 1):
            plan.append({**copy.deepcopy(case), "repeat": repeat})
    return plan


def summarize(rows):
    completed = [row for row in rows if isinstance(row.get("observed_accept"), bool)]
    summary = {
        "planned": len(rows),
        "completed": len(completed),
        "errors": sum("error_type" in row for row in rows),
        "matched": sum(row["observed_accept"] == row["expected_accept"] for row in completed),
        "false_accept": sum(row["observed_accept"] and not row["expected_accept"] for row in completed),
        "false_reject": sum(not row["observed_accept"] and row["expected_accept"] for row in completed),
        "error_types": dict(
            sorted(Counter(row["error_type"] for row in rows if "error_type" in row).items())
        ),
        "schema_error_types": dict(
            sorted(Counter(error["type"] for row in rows for error in row.get("schema_errors", [])).items())
        ),
    }
    reason_counts = Counter(
        mark.removeprefix("reason_")
        for row in completed
        for mark in row.get("marks", [])
        if mark.startswith("reason_")
    )
    summary["observed_rejection_reasons"] = dict(sorted(reason_counts.items()))
    reason_cases = [row for row in completed if row.get("expected_reason_codes")]
    reason_mismatches = []
    for row in reason_cases:
        observed = sorted(
            mark.removeprefix("reason_") for mark in row.get("marks", []) if mark.startswith("reason_")
        )
        expected = sorted(row["expected_reason_codes"])
        if not set(expected).issubset(observed):
            reason_mismatches.append({"case_id": row["case_id"], "expected": expected, "observed": observed})
    summary["reason_cases"] = len(reason_cases)
    summary["reason_covered"] = len(reason_cases) - len(reason_mismatches)
    summary["reason_mismatches"] = reason_mismatches
    finding_cases = [row for row in completed if "expected_unsupported_check_count" in row]
    summary["fact_finding_cases"] = len(finding_cases)
    summary["fact_finding_matched"] = sum(
        sum(check.get("supported") is False for check in row.get("review", {}).get("checks", []))
        == row["expected_unsupported_check_count"]
        for row in finding_cases
    )
    pairs = defaultdict(list)
    for row in completed:
        if row.get("pair_id"):
            pairs[row["pair_id"]].append(row)
    summary["pairs"] = {
        pair_id: {
            "factor": values[0].get("factor"),
            "completed": len(values),
            "matched": sum(v["observed_accept"] == v["expected_accept"] for v in values),
            "observed_accept": sum(v["observed_accept"] for v in values),
        }
        for pair_id, values in sorted(pairs.items())
    }
    delivery_cases = [row for row in completed if "expected_expects_reply" in row]
    summary["delivery_cases"] = len(delivery_cases)
    summary["delivery_matched"] = sum(
        row.get("review", {}).get("delivery", {}).get("expects_reply") == row["expected_expects_reply"]
        for row in delivery_cases
    )
    addressee_cases = [row for row in completed if "expected_addressee_id" in row]
    summary["addressee_cases"] = len(addressee_cases)
    summary["addressee_matched"] = sum(
        row.get("review", {}).get("delivery", {}).get("addressee_id") == row["expected_addressee_id"]
        for row in addressee_cases
    )
    action_cases = [row for row in completed if "expected_requires_player_action" in row]
    summary["action_prerequisite_cases"] = len(action_cases)
    summary["action_prerequisite_matched"] = sum(
        row.get("review", {}).get("delivery", {}).get("requires_player_action")
        == row["expected_requires_player_action"]
        for row in action_cases
    )
    action_cases = [row for row in completed if "expected_requires_player_action" in row]
    summary["action_prerequisite_cases"] = len(action_cases)
    summary["action_prerequisite_matched"] = sum(
        row.get("review", {}).get("delivery", {}).get("requires_player_action")
        == row["expected_requires_player_action"]
        for row in action_cases
    )
    return summary


def safe_schema_errors(exc):
    """Record only bounded schema locations/types, never reviewer output or validation values."""
    safe = []
    for error in exc.errors[:8]:
        path, kind = error.get("path"), error.get("type")
        if not isinstance(path, str) or not isinstance(kind, str):
            continue
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.(?:\d+|[A-Za-z_][A-Za-z0-9_]*))*", path):
            path = ""
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", kind):
            kind = "unknown"
        safe.append({"path": path[:96], "type": kind})
    return safe


def explanation_plan(plan, explanation, *, assignment_target=False):
    if explanation not in {"full", "concise", "paired"} or (assignment_target and explanation != "full"):
        raise ValueError("Explanation comparison requires speech calibration")
    expanded = [
        {**case, "review_explanation": mode}
        for index, case in enumerate(plan)
        for mode in (("full", "concise") if index % 2 == 0 else ("concise", "full"))
        if explanation == "paired" or mode == explanation
    ]
    if len(expanded) > 24:
        raise ValueError("Calibration is limited to 24 planned calls including explanation pairs")
    return expanded


def response_binding_plan(plan, mode, *, assignment_target=False, explanation="full"):
    if mode not in {"off", "on", "paired"} or (mode != "off" and (assignment_target or explanation != "full")):
        raise ValueError("Response binding requires full speech calibration")
    if mode != "off" and any(response_source(case.get("director_assignment")) is None for case in plan):
        raise ValueError("Every binding case requires an actual answer_only response source")
    expanded = [{**case, "review_response_binding": enabled} for index, case in enumerate(plan)
        for enabled in ([False, True] if index % 2 == 0 else [True, False])
        if mode == "paired" or enabled == (mode == "on")]
    if len(expanded) > 24:
        raise ValueError("Calibration is limited to 24 planned calls including response-binding pairs")
    return expanded


def response_binding_summary(rows):
    """Keep provider verdicts separate from deterministic post-review rejection."""
    provider_rows = [{**row, "review": row.get("provider_review", row.get("review", {})),
        "marks": sorted(semantic_review_marks(row.get("provider_review", row.get("review", {})))),
        "observed_accept": row["provider_observed_accept"]} for row in rows
        if isinstance(row.get("provider_observed_accept"), bool)]
    assessments = [row for row in rows if row.get("review_response_binding")
        and isinstance(row.get("provider_review", {}).get("response_assessment"), dict)]
    return {
        "provider_verdict": summarize(provider_rows),
        "after_runtime_gate": summarize(rows),
        "gate_changed_accept_to_reject": sum(row.get("provider_observed_accept") is True
            and row.get("observed_accept") is False for row in rows),
        "binding_issues": dict(sorted(Counter(issue for row in rows
            for issue in row.get("binding_issues", [])).items())),
        "assessment_statuses": dict(sorted(Counter(row["provider_review"]["response_assessment"]["status"]
            for row in assessments).items())),
        "expected_status_cases": sum("expected_response_status" in row for row in assessments),
        "expected_status_matched": sum(row["provider_review"]["response_assessment"]["status"]
            == row["expected_response_status"] for row in assessments if "expected_response_status" in row),
    }


async def run(args):
    data = args.cases.read_bytes()
    suite = json.loads(data)
    plan = materialize_cases(suite, args.case_id, args.repeats)
    assignment_target = suite.get("target") == "assignment"
    role = "director_assignment" if assignment_target else "actor_review"
    schema = AssignmentReview if assignment_target else ActorReview
    clause_coverage = bool(getattr(args, "assignment_coverage", False))
    explanation = getattr(args, "review_explanation", "full")
    plan = explanation_plan(plan, explanation, assignment_target=assignment_target)
    response_binding = getattr(args, "response_binding", "off")
    plan = response_binding_plan(plan, response_binding, assignment_target=assignment_target, explanation=explanation)
    experiment_cap = getattr(args, "max_spend_usd", None)
    if experiment_cap is not None and not 0 < experiment_cap <= 1.2:
        raise ValueError("Calibration cap must be within 0-1.20 USD")
    if clause_coverage and not assignment_target:
        raise ValueError("Assignment coverage is only defined for assignment calibration")
    config = Settings()
    db = Database(config)
    runtime = Runtime(db, config)
    provider = Provider(db, config, runtime)
    report = {
        "format": "sceneweaver-reviewer-calibration-result",
        "suite_sha256": hashlib.sha256(data).hexdigest(),
        "review_system_sha256": hashlib.sha256(
            candidate_messages(
                plan[0], clause_coverage=clause_coverage, explanation=plan[0]["review_explanation"],
                response_binding=plan[0]["review_response_binding"],
            )[0]["content"].encode()
        ).hexdigest(),
        "review_system_sha256_by_explanation": {
            mode: hashlib.sha256(
                candidate_messages(plan[0], clause_coverage=clause_coverage, explanation=mode,
                                   response_binding=plan[0]["review_response_binding"])[0][
                    "content"
                ].encode()
            ).hexdigest()
            for mode in sorted({case["review_explanation"] for case in plan})
        },
        "message_builder_source_sha256": {
            name: hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
            for name in (
                "src/chatbot/npc/audit.py",
                "src/chatbot/npc/review.py",
                "src/chatbot/npc/response_review.py",
                "src/chatbot/role_policy.py",
                "src/chatbot/npc/assignment.py",
            )
        },
        "started_at": now().isoformat(),
        "cases": [],
        "suite_id": suite.get("suite_id"),
        "suite_version": suite.get("version"),
        "target": "assignment" if assignment_target else "speech",
        "assignment_coverage": clause_coverage,
        "review_explanation": explanation,
        "response_binding": response_binding,
        "review_system_sha256_by_response_binding": {
            "on" if enabled else "off": hashlib.sha256(candidate_messages(plan[0],
                clause_coverage=clause_coverage, explanation=plan[0]["review_explanation"],
                response_binding=enabled)[0]["content"].encode()).hexdigest()
            for enabled in sorted({case["review_response_binding"] for case in plan})
        },
        "experiment_cap_usd": experiment_cap,
        "planned_runs": [f"{c['case_id']}#{c['repeat']}:{c['review_explanation']}:binding-"
                         f"{'on' if c['review_response_binding'] else 'off'}" for c in plan],
        "scope": "synthetic development calibration; reviewer only, no world commits",
        "route": {
            "role": role,
            "model": (
                "deepseek-flash"
                if role == "actor_review"
                and role not in config.openrouter_roles
                and config.model_provider == "deepseek"
                and config.deepseek_review_format == "responses_json_schema"
                and config.dialogue_model == "deepseek-v4-flash"
                else config.openrouter_roles.get(role, config.dialogue_model)
            ),
            "provider": "openrouter" if role in config.openrouter_roles else config.model_provider,
            "output_format": (
                "openrouter_json_schema"
                if role in config.openrouter_roles
                else config.deepseek_review_format
                if config.model_provider == "deepseek" and role == "actor_review"
                else "json_object"
            ),
        },
    }

    def spent():
        with db.session() as s:
            return float(
                s.scalar(
                    select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)).where(
                        Usage.created_at >= now().replace(hour=0, minute=0, second=0, microsecond=0)
                    )
                )
            )

    def save():
        report["summary"] = summarize(report["cases"])
        if explanation == "paired":
            report["by_explanation"] = {
                mode: summarize([row for row in report["cases"] if row["review_explanation"] == mode])
                for mode in ("full", "concise")
            }
        if response_binding == "paired":
            report["by_response_binding"] = {
                "on" if enabled else "off": response_binding_summary([row for row in report["cases"]
                    if row["review_response_binding"] == enabled]) for enabled in [False, True]}
        report["summary"].update(
            planned=len(plan),
            attempted=len(report["cases"]),
            not_attempted=len(plan) - len(report["cases"]),
        )
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")

    report["global_day_before_usd"] = spent()
    original_turn_budget = config.turn_budget_usd
    save()
    try:
        for case in plan:
            if experiment_cap is not None:
                remaining = experiment_cap - max(0, spent() - report["global_day_before_usd"])
                if remaining <= 0:
                    report["stopped"] = "experiment budget exhausted"
                    break
                # Concurrent project spending reduces headroom conservatively;
                # normal provider reservations also enforce this remaining cap.
                config.turn_budget_usd = min(original_turn_budget, remaining)
            if spent() + config.turn_budget_usd > config.daily_budget_usd:
                report["stopped"] = "daily budget headroom"
                break
            row = {
                key: case[key]
                for key in (
                    "case_id",
                    "review_explanation",
                    "review_response_binding",
                    "repeat",
                    "pair_id",
                    "factor",
                    "variant",
                    "expected_accept",
                    "expected_response_status",
                    "expected_expects_reply",
                    "expected_addressee_id",
                    "expected_requires_player_action",
                    "expected_requires_player_action",
                    "expected_reason_codes",
                    "expected_unsupported_check_count",
                    "expected_unsupported_check_count",
                    "rationale",
                )
                if key in case
            }
            report["cases"].append(row)
            messages = candidate_messages(
                case, clause_coverage=clause_coverage, explanation=case["review_explanation"],
                response_binding=case["review_response_binding"],
            )
            row["grounding_candidate_sha256"] = hashlib.sha256(
                json.dumps(messages[1:], ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            row["input_sha256"] = hashlib.sha256(
                json.dumps(messages, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            task = Task(
                id=uid(),
                owner_id=config.workspace_owner,
                kind="review_calibration",
                status="running",
                deadline=now() + timedelta(seconds=180),
                lease_until=now() + timedelta(minutes=10),
                lease_version=1,
                lease_owner="calibration",
                attempt=1,
                payload={},
            )
            with db.session.begin() as s:
                s.add(task)
            row["task_id"] = task.id
            save()
            trace = TimingTrace(task)
            try:
                with trace.activate(), measure("calibration.review"):
                    annotate(review_explanation_mode=case["review_explanation"])
                    review = await provider.chat(
                        task.id,
                        role,
                        messages,
                        schema=BoundActorReview if case["review_response_binding"] else schema,
                        max_tokens=1000,
                        operation="calibration",
                    )
                if assignment_target:
                    raw_review = copy.deepcopy(review)
                    validated = validate_assignment_evidence(
                        AssignmentReview.model_validate(review), case["info"]
                    )
                    if clause_coverage:
                        validated = validate_assignment_coverage(
                            validated, assignment_units(case["assignment"])
                        )
                    review = validated.model_dump()
                    row["provider_review"] = raw_review
                if case["review_response_binding"]:
                    row["provider_review"] = copy.deepcopy(review)
                    review, row["binding_issues"] = enforce_binding(review, case["director_assignment"], case["speech"]["text"])
                row["provider_observed_accept"] = "semantic_accepted" in semantic_review_marks(row.get("provider_review", review))
                marks = sorted(semantic_review_marks(review))
                observed_reasons = sorted(
                    mark.removeprefix("reason_") for mark in marks if mark.startswith("reason_")
                )
                row.update(
                    review=review,
                    marks=marks,
                    observed_accept="semantic_accepted" in marks,
                    observed_reason_codes=observed_reasons,
                )
                row["runtime_overrode"] = row["provider_observed_accept"] and not row["observed_accept"]
                if case["review_response_binding"] and row["observed_accept"] and not case["expected_accept"]:
                    report["stopped"] = "response-binding false acceptance; remaining cases not attempted"
                runtime.finish(task.id, 1, {"calibration": True})
            except Exception as exc:
                row["error_type"] = type(exc).__name__
                if isinstance(exc, StructuredOutputError):
                    row["schema_errors"] = safe_schema_errors(exc)
                    try:
                        json.loads(exc.answer)
                    except (TypeError, ValueError):
                        row["parseable_json"] = False
                    else:
                        row["parseable_json"] = True
                if isinstance(exc, TransientProviderError):
                    report["stopped"] = "transport failure; remaining cases not attempted"
                if isinstance(exc, BudgetExceeded):
                    report["stopped"] = "budget headroom; remaining cases not attempted"
                if response_binding != "off" and not report.get("stopped"):
                    report["stopped"] = "response-binding experiment error; remaining cases not attempted"
                with db.session.begin() as s:
                    s.get(Task, task.id).status = "failed"
            finally:
                trace.save(db)
                row["duration_ms"] = trace.duration_ms
                with db.session() as s:
                    usage_rows = list(s.scalars(select(Usage).where(Usage.task_id == task.id)))
                    row["model_attempts"] = len(usage_rows)
                    row["input_tokens"] = sum(row.input_tokens for row in usage_rows)
                    row["output_tokens"] = sum(row.output_tokens for row in usage_rows)
                    row["usage_statuses"] = dict(Counter(row.status for row in usage_rows))
                    row["cost_usd"] = float(
                        s.scalar(
                            select(func.coalesce(func.sum(Usage.cost), 0)).where(Usage.task_id == task.id)
                        )
                    )
                save()
            if report.get("stopped"):
                break
    finally:
        report["global_day_after_usd"] = spent()
        report["finished_at"] = now().isoformat()
        save()
        await provider.close()
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-paid", action="store_true")
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--assignment-coverage", action="store_true")
    parser.add_argument("--review-explanation", choices=["full", "concise", "paired"], default="full")
    parser.add_argument("--response-binding", choices=["off", "on", "paired"], default="off")
    parser.add_argument("--max-spend-usd", type=float, help="Optional conservative batch cap, additionally to the daily cap")
    args = parser.parse_args()
    if not args.run_paid or args.output.exists():
        parser.error("Require --run-paid and a new output path")
    asyncio.run(run(args))
