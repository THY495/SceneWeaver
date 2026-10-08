"""Bounded official-model preflight miss comparison and actual player-route flow.

Clone one initialized synthetic ferry save. Only preflight differs in the two
ordinary-question arms. Direction is fixed to one grounded reply; generation,
review, completion and routing are real. No TTS/browser/native-game claim.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from probe_actor_pipeline import reserve_probe
from probe_beat_reuse import ROOT, apply_player_continuation, fixture, report, route_steps
from sqlalchemy import create_engine, func, select, text

from chatbot.api import create_app
from chatbot.config import Settings
from chatbot.directing import DirectorPlan
from chatbot.npc import director
from chatbot.providers import Provider
from chatbot.runtime import Runtime
from chatbot.storage import Database, Story, Usage
from chatbot.story_blueprints import Recipe
from chatbot.worker import Worker

QUESTION = "沈舟，你在码头做什么工作？"
JOB_ID = "answer-player-" + "x" * 66  # Exercise the valid 80-character audit-key boundary.


def probe_fixture():
    recipe = fixture(exercise_routes=True)
    opening = next(n for n in recipe["narrative"]["nodes"] if n["id"] == "opening")
    opening["direction"] = {
        "version": 1, "objective": "沈舟回应玩家实际提出的问题，然后交还玩家。",
        "constraints": ["角色只说自己知道的事；不替玩家决定去向。"],
        "beats": [{"id": "known-profile-answer", "title": "回应玩家",
            "objective": "沈舟回应玩家实际提出的问题。", "participants": ["keeper"],
            "completion": "沈舟已经回应当前玩家问题。", "handoff": "after_beat", "max_segments": 1}],
    }
    return Recipe.model_validate(recipe).model_dump(mode="json")


async def question_plan(provider, task_id, direction, beat_id, **kwargs):
    cast = json.loads(kwargs["public_context"])["cast"]
    actor = next(p["id"] for p in cast if p["name"] == "沈舟")
    player = next(p["id"] for p in cast if p["name"] not in {"阿岚", "沈舟", "林澈"})
    if any(job["actor"] == actor for job in kwargs.get("completed_jobs", [])):
        jobs = []  # Recovery must not repeat a committed reply.
    else:
        source = kwargs["current_player_event_id"]
        if not source:
            raise ValueError("Requires an actual submitted player utterance")
        jobs = [{"id": JOB_ID, "actor": actor, "addressees": [player],
                 "grounded_response": {"event_id": source, "delivery": "brief"}}]
    return DirectorPlan(beat_id=beat_id, jobs=jobs, handoff=True,
                        reason="固定单一真实发言回应，只比较行动前置开关。")


def save(directory, name, value):
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def measured_turn(worker, player, story_id, message):
    begun = time.monotonic()
    worker.runtime.submit(player, worker.config.workspace_owner, message, "preflight-" + uuid4().hex)
    task = worker.runtime.claim("preflight-probe")
    if not task:
        raise RuntimeError("No submitted player task claimed")
    worker_start = time.monotonic()
    await worker.run_task(task)
    result = report(worker.db, task, time.monotonic() - worker_start)
    result.update(player_input=message, submit_claim_s=round(worker_start - begun, 3),
                  submit_to_worker_complete_s=round(time.monotonic() - begun, 3))
    apply_start = time.monotonic()
    result["player_continuation"] = apply_player_continuation(worker.config, worker.db, story_id)
    result["player_api_continue_s"] = round(time.monotonic() - apply_start, 3)
    with worker.db.session() as s:
        result["narrative_after_continuation"] = s.get(Story, story_id).settings.get("narrative", {})
    result["submit_to_continue_complete_s"] = round(time.monotonic() - begun, 3)
    result["preflight_spans"] = [span for timing in result["timings"] for span in timing.get("spans", [])
                                 if span["name"] == "npc.player_action_preflight"]
    # Human assessment must inspect exact replies, not equate a commit with a useful answer.
    result["effective_answer_assessment"] = "pending_manual_review" if result["segments"] else "no_npc_text"
    return result


async def run_probe(origin, admin, base, directory, cap, *, question_only=False):
    names, total, seed_cost = [], 0.0, 0.0
    original_plan = director.propose_plan
    seed = "sceneweaver_preflight_" + uuid4().hex[:10] + "_test"
    summary = {"scope": "Official DeepSeek; cloned source API save; fixed one-NPC grounded question direction; real actor/review/routing; no TTS/browser/native game",
               "ordinary_question": QUESTION, "model": origin.dialogue_model, "cap_usd": cap,
               "question_only": question_only,
               "ordinary_arms": [], "route_flow": []}

    def create(name, template=None):
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
        names.append(name)

    def configured(name, enabled):
        return origin.model_copy(update={"product": "roleplay", "database_url": base + "/" + name,
            "data_dir": directory / (name + "-data"), "daily_budget_usd": seed_cost + (cap - seed_cost) / 3,
            "turn_budget_usd": (cap - seed_cost) / 9, "max_calls_per_task": 32,
            "embedding_provider": "lexical", "tts_enabled": False, "acknowledgement_enabled": False,
            "openrouter_roles": {}, "role_actor_generation": "combined_context",
            "role_assignment_authorization": "grounded_response", "role_director_beat_reuse": True,
            "role_player_action_preflight": enabled})

    try:
        create(seed)
        config = configured(seed, False)
        db = Database(config)
        db.initialize()
        api = Provider(db, config, Runtime(db, config))
        try:
            recipe = probe_fixture()
            save(directory, "fixture.json", recipe)
            summary["fixture_sha256"] = hashlib.sha256(json.dumps(recipe, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            with TestClient(create_app(config, db, api)) as client:
                blueprint = str(uuid4())
                client.put("/api/role/experiments/blueprints/" + blueprint,
                           json={"expected_version": 0, "recipe": recipe}).raise_for_status()
                started = client.post("/api/role/experiments/start", json={"request_id": blueprint,
                    "blueprint_id": blueprint, "blueprint_version": 1})
                started.raise_for_status()
                story_id, player = started.json()["story_id"], started.json()["player_id"]
                client.put(f"/api/role/stories/{story_id}/settings", json={"paused": False}).raise_for_status()
            # Normal start runs a budgeted readiness request. Cloned Usage rows
            # are shared history, not newly dispatched/billed in each arm.
            with db.session() as s:
                seed_usage = list(s.scalars(select(Usage)))
                seed_cost = float(s.scalar(select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0))))
            total = seed_cost
            summary["seed"] = {"story_id": story_id, "player_id": player, "model_calls": len(seed_usage),
                "cost_usd": seed_cost, "usage": [{"role": row.role, "model": row.model,
                    "status": row.status, "cost_usd": row.cost, "reserved_usd": row.reserved} for row in seed_usage]}
        finally:
            await api.close()
            db.close()
        director.propose_plan = question_plan
        # Verify the previously blocked route before any question-generation failure.
        arms = [("ordinary-off", False), ("ordinary-on", True)]
        if not question_only:
            arms.insert(0, ("route-flow", True))
        for label, enabled in arms:
            name = "sceneweaver_preflight_" + uuid4().hex[:10] + "_test"
            create(name, seed)
            config = configured(name, enabled)
            db, worker = Database(config), None
            try:
                worker = Worker(db, config)
                messages = [("opening", QUESTION)] if label != "route-flow" else route_steps(True)
                for index, (expected, message) in enumerate(messages):
                    result = await measured_turn(worker, player, story_id, message)
                    frame = result["player_continuation"]
                    reached = result["status"] == "completed" and result["narrative_after_continuation"].get("node") == expected
                    ending = expected == "wait"
                    question_valid = bool(result["status"] == "completed" and result["segments"]
                        and all(segment["actor"] == "沈舟" for segment in result["segments"])) if label != "route-flow" else None
                    preflight_valid = bool(reached and not result["segments"]
                        and result["auto_route"].get("scope") == "player_action"
                        and result["auto_route"].get("task_id") == result["task_id"]
                        and set(result["model_calls"]) == {"scene_routing"}) if label == "route-flow" else None
                    result.update(preflight_enabled=enabled, expected_node=expected, reached_expected=reached,
                        question_run_valid=question_valid, route_preflight_valid=preflight_valid,
                        ending_verified=bool(reached and frame.get("ending") and frame.get("finished_at")
                            and frame.get("execution", {}).get("phase") not in {"opening", "video"}
                            and not result["pending_reply_ids"]) if ending else None)
                    file = f"{label}-{index + 1}.json"
                    save(directory, file, result)
                    compact = {key: result[key] for key in ["status", "turn_s", "first_committed_text_s",
                        "cost_usd", "model_calls", "expected_node", "reached_expected", "ending_verified",
                        "submit_to_continue_complete_s", "player_api_continue_s",
                        "question_run_valid", "route_preflight_valid"]}
                    compact.update(report=file, preflight_enabled=enabled)
                    summary["ordinary_arms" if label != "route-flow" else "route_flow"].append(compact)
                    print(json.dumps(compact, ensure_ascii=False), flush=True)
                    summary["total_cost_usd"] = total + result["database_cost_usd"] - seed_cost
                    save(directory, "summary.json", summary)
                    if (not reached or (ending and not result["ending_verified"])
                            or question_valid is False or preflight_valid is False):
                        total += result["database_cost_usd"] - seed_cost
                        summary.update(total_cost_usd=total, stopped_on_failure=file)
                        save(directory, "summary.json", summary)
                        return total  # Stop the whole paid package, retaining its actual/estimated cost.
                total += result["database_cost_usd"] - seed_cost
            finally:
                if worker:
                    await worker.provider.close()
                db.close()
        summary["total_cost_usd"] = total
        save(directory, "summary.json", summary)
        return total
    finally:
        director.propose_plan = original_plan
        for name in reversed(names):
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-paid", action="store_true")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-spend-usd", type=float, default=0.30)
    parser.add_argument("--question-only", action="store_true", help="Do not repeat an already verified route flow")
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    if not args.run_paid or not directory.is_relative_to(ROOT / ".runtime") or directory.exists():
        parser.error("Explicit paid opt-in and a fresh directory inside .runtime are required")
    if not 0 < args.max_spend_usd <= 0.40:
        parser.error("Bounded cap must be within 0-0.40 USD")
    origin = Settings()
    if origin.api_base.rstrip("/") != "https://api.deepseek.com" or not origin.generation_key():
        parser.error("Only previously authorized official DeepSeek credentials are allowed")
    if origin.daily_budget_usd != 6:
        parser.error("Expected authorized daily 6 USD cap")
    base, name = os.environ.get("TEST_DATABASE_URL",
        "postgresql+psycopg://continuum:continuum_local@127.0.0.1:55432/continuum_test").rsplit("/", 1)
    if not name.endswith("_test") or "@127.0.0.1:" not in base or not base.startswith("postgresql+psycopg://"):
        parser.error("Only a local explicitly named *_test server is allowed")
    directory.mkdir(parents=True)
    project = Database(origin)
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
    reservation = reserve_probe(project, origin, args.max_spend_usd)
    save(directory, "reservation.json", {"id": reservation, "reserved_usd": args.max_spend_usd})
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        cost = asyncio.run(run_probe(origin, admin, base, directory, args.max_spend_usd,
                                    question_only=args.question_only))
        with project.session.begin() as s:
            row = s.get(Usage, reservation)
            row.cost, row.reserved, row.status = cost, 0, "completed"
    finally:
        # Uncertain failures retain the reservation instead of claiming zero cost.
        admin.dispose()
        project.close()


if __name__ == "__main__":
    main()
