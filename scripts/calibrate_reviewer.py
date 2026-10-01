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
from chatbot.npc.review import REVIEW_SYSTEM, review_messages
from chatbot.observability.timing import TimingTrace
from chatbot.providers import Provider, StructuredOutputError, TransientProviderError
from chatbot.role_policy import semantic_review_marks
from chatbot.runtime import Runtime
from chatbot.schemas import ActorReview
from chatbot.storage import Database, Task, Usage, now, uid


def candidate_messages(case):
    return review_messages(
        case["info"],
        [],
        case["speech"],
        case["sources"],
        actor_id="npc",
        documents=[],
        intent=case["intent"],
        player_request="",
        cast=case["cast"],
    )


def materialize_cases(suite, selected_ids=None, repeats=1):
    """Freeze the complete call plan before any paid request is made."""
    defaults = suite.get("defaults", {})
    cases = [{**copy.deepcopy(defaults), **copy.deepcopy(case)} for case in suite["cases"]]
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
    required = {"info", "speech", "sources", "intent", "cast", "expected_accept"}
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


async def run(args):
    data = args.cases.read_bytes()
    suite = json.loads(data)
    plan = materialize_cases(suite, args.case_id, args.repeats)
    config = Settings()
    db = Database(config)
    runtime = Runtime(db, config)
    provider = Provider(db, config, runtime)
    report = {
        "format": "sceneweaver-reviewer-calibration-result",
        "suite_sha256": hashlib.sha256(data).hexdigest(),
        "review_system_sha256": hashlib.sha256(REVIEW_SYSTEM.encode()).hexdigest(),
        "started_at": now().isoformat(),
        "cases": [],
        "suite_id": suite.get("suite_id"),
        "suite_version": suite.get("version"),
        "planned_runs": [f"{c['case_id']}#{c['repeat']}" for c in plan],
        "scope": "synthetic development calibration; reviewer only, no world commits",
        "route": {
            "role": "actor_review",
            "model": (
                "deepseek-flash"
                if "actor_review" not in config.openrouter_roles
                and config.model_provider == "deepseek"
                and config.deepseek_review_format == "responses_json_schema"
                and config.dialogue_model == "deepseek-v4-flash"
                else config.openrouter_roles.get("actor_review", config.dialogue_model)
            ),
            "provider": "openrouter" if "actor_review" in config.openrouter_roles else config.model_provider,
            "output_format": (
                "openrouter_json_schema"
                if "actor_review" in config.openrouter_roles
                else config.deepseek_review_format
                if config.model_provider == "deepseek"
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
        report["summary"].update(
            planned=len(plan),
            attempted=len(report["cases"]),
            not_attempted=len(plan) - len(report["cases"]),
        )
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")

    report["global_day_before_usd"] = spent()
    save()
    try:
        for case in plan:
            if spent() + config.turn_budget_usd > min(2.0, config.daily_budget_usd):
                report["stopped"] = "daily budget headroom"
                break
            row = {
                key: case[key]
                for key in (
                    "case_id",
                    "repeat",
                    "pair_id",
                    "factor",
                    "variant",
                    "expected_accept",
                    "expected_expects_reply",
                    "expected_reason_codes",
                    "rationale",
                )
                if key in case
            }
            report["cases"].append(row)
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
                with trace.activate():
                    review = await provider.chat(
                        task.id,
                        "actor_review",
                        candidate_messages(case),
                        schema=ActorReview,
                        max_tokens=1000,
                        operation="calibration",
                    )
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
                with db.session.begin() as s:
                    s.get(Task, task.id).status = "failed"
            finally:
                trace.save(db)
                row["duration_ms"] = trace.duration_ms
                with db.session() as s:
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
    args = parser.parse_args()
    if not args.run_paid or args.output.exists():
        parser.error("Require --run-paid and a new output path")
    asyncio.run(run(args))
