"""Paired real-model three-NPC probe; identical fixture, unchanged reviews, no TTS.

Uses isolated local *_test databases. Reserves the whole probe cap in the
project usage ledger before any API call, so isolation cannot bypass its cap.
"""

import argparse
import asyncio
import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from accept_directed_ferry_live import ROOT, run
from sqlalchemy import create_engine, func, select, text

from chatbot.config import Settings
from chatbot.directing import DirectorPlan
from chatbot.providers import operation_cache_name
from chatbot.storage import Conversation, Database, Operation, SceneRecord, Usage, now, uid


async def fixed_plan(provider, task_id, direction, beat_id, **kwargs):
    """Fixed public three-job control, so the actor experiment cannot change direction."""
    cast = json.loads(kwargs["public_context"])["cast"]
    names = {person["name"]: person["id"] for person in cast}
    alan, shen, lin = (names[name] for name in ["阿岚", "沈舟", "林澈"])
    completed_actors = {job["actor"] for job in kwargs.get("completed_jobs", [])}
    jobs = []
    for index, (actor, target, objective) in enumerate(
        [
            (alan, shen, "表达自己对今晚渡船的看法，说明自己知道和不知道的部分。"),
            (shen, alan, "回应阿岚刚才实际说过的话，表达自己的态度和已知与未知。"),
            (lin, shen, "回应沈舟刚才实际说过的话，表达自己的看法并自然收尾。"),
        ]
    ):
        if actor in completed_actors:
            continue
        jobs.append(
            {
                "id": f"voice-{index}",
                "actor": actor,
                "addressees": [target],
                "objective": objective,
                "depends_on": [jobs[-1]["id"]] if jobs else [],
                "delivery_constraints": ["简短一至两句；不新增需要本段答复的问题。"],
            }
        )
    return DirectorPlan(beat_id=beat_id, jobs=jobs, handoff=True, reason="固定编排，仅对照演员生成链路。")


async def fixed_grounded_plan(provider, task_id, direction, beat_id, **kwargs):
    """Both authorization arms receive identical typed jobs, never simpler prose."""
    names = {person["name"]: person["id"] for person in json.loads(kwargs["public_context"])["cast"]}
    alan, shen, lin = (names[name] for name in ["阿岚", "沈舟", "林澈"])
    completed = {row["actor"]: row["event_id"] for row in kwargs.get("completed_jobs", [])}
    source = {"event_id": kwargs["current_player_event_id"]}
    if not source["event_id"]:
        raise ValueError("The paired fixture requires a real player utterance")
    jobs = []
    for index, (actor, target) in enumerate([(alan, shen), (shen, alan), (lin, shen)]):
        if actor in completed:
            source = {"event_id": completed[actor]}
            continue
        job = {
            "id": f"voice-{index}",
            "actor": actor,
            "addressees": [target],
            "grounded_response": {**source, "delivery": "brief"},
            "depends_on": [jobs[-1]["id"]] if jobs else [],
        }
        jobs.append(job)
        source = {"job_id": job["id"]}
    return DirectorPlan(
        beat_id=beat_id, jobs=jobs, handoff=True, reason="固定编排，仅对照同一真实发言回应任务的授权方式。"
    )


def normalize_assignment(services, task, actor, assignment):
    """Remove per-run UUIDs, retaining exact wording and all actual requirements."""
    with services.db.session() as s:
        names = {
            person.id: person.current_state["profile"]["name"] for person in s.scalars(select(Conversation))
        }
        source = assignment["response_utterance"]
        source_id = (
            "player-input"
            if source["event_id"] == task.payload.get("event_id")
            else "committed:" + names.get(source["speaker_id"], "unknown")
        )
    canonical = {
        **assignment,
        "addressees": [names.get(value, value) for value in assignment["addressees"]],
        "response_utterance": {
            **source,
            "event_id": source_id,
            "speaker_id": names.get(source["speaker_id"], source["speaker_id"]),
        },
    }
    return {
        "actor": names[actor.id],
        "assignment": canonical,
        "sha256": hashlib.sha256(
            json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }


def reserve_probe(db, config, cap):
    with db.session.begin() as s:
        s.execute(text("SELECT pg_advisory_xact_lock(731291)"))
        day = now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        spent = s.scalar(
            select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)).where(Usage.created_at >= day)
        )
        if spent + cap > config.daily_budget_usd:
            raise RuntimeError("Insufficient daily budget for the bounded probe")
        row = Usage(
            id=uid(),
            owner_id=config.workspace_owner,
            task_id=uid(),
            role="architecture_probe",
            model=config.dialogue_model,
            reserved=cap,
        )
        s.add(row)
        return row.id


async def paired(origin, admin, base, directory, pairs, cap, freeze_plan, authorization_probe=False):
    from chatbot.npc import directed_turn, director

    original_plan = director.propose_plan
    original_authorize = directed_turn.authorize_assignment
    captured_assignments = []

    async def capture(*args, **kwargs):
        from chatbot.npc.grounded_assignment import compile_response

        services, task, turn, actor, job = args
        _, info = services.context.assemble(task, actor_id=actor.id)
        compiled, _ = compile_response(services, task, turn, actor, job, info)
        captured = {**normalize_assignment(services, task, actor, compiled), "authorization_completed": False}
        captured_assignments.append(captured)
        assignment = await original_authorize(*args, **kwargs)
        if normalize_assignment(services, task, actor, assignment)["sha256"] != captured["sha256"]:
            raise RuntimeError("Authorization changed the paired actual task")
        captured["authorization_completed"] = True
        return assignment

    if authorization_probe:
        director.propose_plan = fixed_grounded_plan
        directed_turn.authorize_assignment = capture
    elif freeze_plan:
        director.propose_plan = fixed_plan
    results = []
    for pair in range(pairs):
        modes = ["semantic", "grounded_response"] if authorization_probe else ["separate", "combined_context"]
        if pair % 2:
            modes.reverse()
        for mode in modes:
            captured_assignments.clear()
            name = "sceneweaver_actor_" + uuid4().hex[:10] + "_test"
            with admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{name}"'))
            output = directory / f"pair-{pair + 1}-{mode}.json"
            config = origin.model_copy(
                update={
                    "product": "roleplay",
                    "database_url": base + "/" + name,
                    "data_dir": directory / (output.stem + "-data"),
                    "daily_budget_usd": cap / (2 * pairs),
                    "turn_budget_usd": 0.15,
                    "max_calls_per_task": 48,
                    "embedding_provider": "lexical",
                    "tts_enabled": False,
                    "acknowledgement_enabled": False,
                    "openrouter_roles": {},
                    "role_actor_generation": "separate" if authorization_probe else mode,
                    "role_assignment_authorization": mode if authorization_probe else "semantic",
                }
            )
            db = Database(config)
            try:
                db.initialize()
                report = await run(config, db, output)
                with db.session() as s:
                    rows = list(
                        s.scalars(
                            select(SceneRecord)
                            .where(
                                SceneRecord.task_id == report["task_id"],
                                SceneRecord.kind == "speech",
                                SceneRecord.actor_id.in_(report["expected_actor_ids"]),
                            )
                            .order_by(SceneRecord.created_at)
                        )
                    )
                    timings = [
                        op.payload["timing_v1"]
                        for op in s.scalars(select(Operation))
                        if op.task_id == report["task_id"] and "timing_v1" in op.payload
                    ]
                    starts = [t["started_at"] for t in timings if t.get("started_at")]
                    started = min(datetime.fromisoformat(value) for value in starts) if starts else None
                    first = (
                        round((rows[0].created_at - started).total_seconds(), 3) if rows and started else None
                    )
                    report.update(
                        actor_generation=config.role_actor_generation,
                        model=config.dialogue_model,
                        assignment_authorization=config.role_assignment_authorization,
                        normalized_assignments=list(captured_assignments),
                        first_committed_text_s=first,
                        timing_records=timings,
                        fixed_director_plan=freeze_plan,
                        all_usage_usd=float(
                            s.scalar(select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)))
                        ),
                        generation_spans=[
                            span
                            for t in timings
                            for span in t.get("spans", [])
                            if "actor_generation" in span.get("attributes", {})
                            or "combined_speech_reused" in span.get("attributes", {})
                        ],
                        candidate_audit=[
                            {"operation": op.id, "cache_operation": operation_cache_name(op), "value": op.payload["value"]}
                            for op in s.scalars(
                                select(Operation).where(Operation.task_id == report["task_id"])
                            )
                            if isinstance(op.payload.get("value"), dict)
                            and any(marker in operation_cache_name(op) for marker in ("actor-turn-", ":review:", ":answer:"))
                        ],
                    )
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                results.append(
                    {
                        "pair": pair + 1,
                        "mode": mode,
                        "model": config.dialogue_model,
                        "passed": report.get("passed"),
                        "first_committed_text_s": first,
                        "turn_s": report["turn_seconds"],
                        "cost_usd": report["all_usage_usd"],
                        "model_calls": dict(Counter(row["role"] for row in report["model_usage"])),
                        "generation_spans": report["generation_spans"],
                        "report": output.name,
                    }
                )
                print(
                    json.dumps(
                        {key: value for key, value in results[-1].items() if key != "generation_spans"},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                (directory / "summary.json").write_text(
                    json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            finally:
                db.close()
                with admin.connect() as conn:
                    conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    director.propose_plan = original_plan
    directed_turn.authorize_assignment = original_authorize
    return sum(row["cost_usd"] for row in results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-paid", action="store_true")
    parser.add_argument("--pairs", type=int, choices=[1, 2], default=1)
    parser.add_argument(
        "--fixed-plan", action="store_true", help="Isolate actor changes with three identical jobs"
    )
    parser.add_argument(
        "--assignment-authorization-probe",
        action="store_true",
        help="Use the same typed three-NPC plan; compare semantic and runtime authorization",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-spend-usd", type=float, default=0.6)
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    if not args.run_paid or not directory.is_relative_to(ROOT / ".runtime") or directory.exists():
        parser.error("Explicit --run-paid and a fresh output directory inside .runtime are required")
    if not 0 < args.max_spend_usd <= 1.2:
        parser.error("Probe cap must be within 0-1.20 USD")
    origin = Settings()
    if origin.api_base.rstrip("/") != "https://api.deepseek.com" or not origin.generation_key():
        parser.error("Previously authorized official DeepSeek credentials are required")
    if origin.daily_budget_usd != 6:
        parser.error("Expected the authorized project daily cap of 6 USD")
    base, name = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+psycopg://continuum:continuum_local@127.0.0.1:55432/continuum_test"
    ).rsplit("/", 1)
    if (
        not name.endswith("_test")
        or "@127.0.0.1:" not in base
        or not base.startswith("postgresql+psycopg://")
    ):
        parser.error("Only a local explicitly named *_test server is allowed")
    directory.mkdir(parents=True)
    project_db = Database(origin)
    admin = create_engine(
        base + "/postgres", isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5}
    )
    reservation = reserve_probe(project_db, origin, args.max_spend_usd)
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        cost = asyncio.run(
            paired(
                origin,
                admin,
                base,
                directory,
                args.pairs,
                args.max_spend_usd,
                args.fixed_plan or args.assignment_authorization_probe,
                args.assignment_authorization_probe,
            )
        )
        with project_db.session.begin() as s:
            row = s.get(Usage, reservation)
            row.cost, row.reserved, row.status = cost, 0, "completed"
    finally:
        # Uncertain failure keeps the reservation rather than pretending it was free.
        admin.dispose()
        project_db.close()


if __name__ == "__main__":
    main()
