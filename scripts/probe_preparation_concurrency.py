"""Bounded width-1/2 scheduling comparison; frozen templates, real commits.

Uses the approved synthetic ferry, official DeepSeek, cloned pre-turn state,
no TTS, no acknowledgement and no prompt tuning. Failed output is a result.
"""

import argparse
import asyncio
import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from accept_directed_ferry_live import ROOT, directed_recipe
from fastapi.testclient import TestClient
from probe_actor_pipeline import reserve_probe
from probe_beat_reuse import report
from sqlalchemy import create_engine, func, select, text

from chatbot.api import create_app
from chatbot.config import Settings
from chatbot.directing import DirectorPlan
from chatbot.npc import directed_turn, director
from chatbot.providers import Provider
from chatbot.runtime import Runtime
from chatbot.storage import Database, Task, Usage, now
from chatbot.story_blueprints import Recipe
from chatbot.worker import Worker

MESSAGE = "阿岚和沈舟先各自向我说明对今晚渡船知道什么、不知道什么；林澈再回应沈舟的实际发言，随后让我接话。"
FROZEN_FILES = [
    "examples/stories/ferry.story.json", "src/chatbot/npc/planning.py", "src/chatbot/npc/performance.py",
    "src/chatbot/npc/combined_generation.py", "src/chatbot/npc/speech_contract.py", "src/chatbot/npc/review.py",
    "src/chatbot/npc/grounded_assignment.py", "src/chatbot/role_policy.py", "src/chatbot/schemas.py",
    "src/chatbot/npc/directed_turn.py", "src/chatbot/config.py", "scripts/probe_preparation_concurrency.py",
    "src/chatbot/npc/director.py", "src/chatbot/npc/direct_response.py", "src/chatbot/npc/speech_packets.py",
    "src/chatbot/directing.py", "src/chatbot/director_state.py", "src/chatbot/director_planning.py",
    "scripts/probe_beat_reuse.py", "scripts/probe_actor_pipeline.py", "scripts/accept_directed_ferry_live.py",
]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def fixture():
    recipe = directed_recipe()
    beat = recipe["narrative"]["nodes"][0]["direction"]["beats"][0]
    beat.update(objective="阿岚和沈舟各自向玩家表达已知与未知；林澈回应沈舟实际发言，然后交还玩家。",
                completion="三名角色都表达过且林澈回应了沈舟。", max_segments=3, handoff="after_beat")
    return Recipe.model_validate(recipe).model_dump(mode="json")


async def fixed_plan(provider, task_id, direction, beat_id, **kwargs):
    names = {person["name"]: person["id"] for person in json.loads(kwargs["public_context"])["cast"]}
    player = next(value for name, value in names.items() if name not in {"阿岚", "沈舟", "林澈"})
    completed = {row["actor"] for row in kwargs.get("completed_jobs", [])}
    # No paid replacement draws after ordinary bad outputs. The existing single
    # recovery may legally yield, but does not invent a different control plan.
    jobs = [
        {"id": "alan", "actor": names["阿岚"], "addressees": [player],
         "grounded_response": {"event_id": kwargs["current_player_event_id"], "delivery": "brief"}},
        {"id": "shen", "actor": names["沈舟"], "addressees": [player],
         "grounded_response": {"event_id": kwargs["current_player_event_id"], "delivery": "brief"}},
        {"id": "lin", "actor": names["林澈"], "addressees": [names["沈舟"]], "depends_on": ["alan", "shen"],
         "grounded_response": {"job_id": "shen", "delivery": "brief"}},
    ] if not completed and not kwargs.get("recovery_reason") else []
    return DirectorPlan(beat_id=beat_id, jobs=jobs, handoff=True,
                        reason="固定两项独立表达与实际提交后的依赖回应；仅比较准备并发度。")


def file_hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FROZEN_FILES}


def write(directory, name, value):
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


def workload_complete(result, expected):
    segments = result["segments"]
    speakers = {segment["actor"]: segment["actor_id"] for segment in segments}
    event_ids = {segment["event_id"] for segment in segments}
    linked = any(
        link.get("source_actor_id") == speakers.get("沈舟")
        and link.get("response_actor_id") == speakers.get("林澈")
        and link.get("verified_committed_link") is True
        and link.get("source_event_id") in event_ids and link.get("response_event_id") in event_ids
        and speakers.get("沈舟") and speakers.get("林澈")
        for link in result["source_authorizations"]
    )
    return bool(result["status"] == "completed" and len(segments) == 3
        and {segment["actor_id"] for segment in segments} == expected
        and linked and result["stop_reason"] != "safety_rejected")


async def run_probe(origin, admin, base, directory, cap, pairs):
    hashes = file_hashes()
    recipe = fixture()
    names, totals, rows = [], {"cost": 0.0, "reserved": 0.0}, []
    seed = "sceneweaver_schedule_" + uuid4().hex[:10] + "_test"
    seed_cost = 0.0
    original_plan, original_authorize = director.propose_plan, directed_turn.authorize_assignment
    identity = {"model": origin.dialogue_model, "model_provider": origin.model_provider,
        "deepseek_review_format": origin.deepseek_review_format, "recipe_sha256": digest(recipe),
        "message_sha256": digest(MESSAGE), "file_hashes": hashes, "cap_usd": cap,
        "pairs": pairs, "paid_director": False, "tts": False, "acknowledgement": False,
        "actor_generation": "combined_context", "assignment_authorization": "grounded_response"}
    identity.update(task_timeout_seconds=240, max_calls_per_arm=48, max_arms=2 * pairs,
                    maximum_task_calls=48 * 2 * pairs, seed_readiness_separately_accounted=True)
    write(directory, "identity.json", identity)

    def create(name, template=None):
        assert name.startswith("sceneweaver_schedule_") and name.endswith("_test") and name.replace("_", "").isalnum()
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
        names.append(name)

    def settings(name, width):
        piece = cap / (1 + 2 * pairs)
        return origin.model_copy(update={"product": "roleplay", "database_url": base + "/" + name,
            "data_dir": directory / "data", "daily_budget_usd": seed_cost + piece, "turn_budget_usd": piece,
            "max_calls_per_task": 48, "embedding_provider": "lexical", "tts_enabled": False,
            "task_timeout": 240,
            "acknowledgement_enabled": False, "openrouter_roles": {}, "role_director_beat_reuse": False,
            "role_player_action_preflight": False, "role_response_review_binding": False,
            "role_actor_generation": "combined_context", "role_assignment_authorization": "grounded_response",
            "role_direct_response": False, "role_speech_representation": "legacy",
            "role_preparation_concurrency": width})

    def collect(db, copied_cost=0):
        with db.session() as s:
            cost = float(s.scalar(select(func.coalesce(func.sum(Usage.cost), 0)))) - copied_cost
            reserved = float(s.scalar(select(func.coalesce(func.sum(Usage.reserved), 0))))
        totals["cost"] += cost
        totals["reserved"] += reserved
        write(directory, "accounting.json", totals)

    try:
        create(seed)
        config = settings(seed, 2)
        db = Database(config)
        api = Provider(db, config, Runtime(db, config))
        try:
            db.initialize()
            with TestClient(create_app(config, db, api)) as client:
                blueprint = str(uuid4())
                client.put("/api/role/experiments/blueprints/" + blueprint,
                    json={"expected_version": 0, "recipe": recipe}).raise_for_status()
                started = client.post("/api/role/experiments/start", json={"request_id": blueprint,
                    "blueprint_id": blueprint, "blueprint_version": 1})
                started.raise_for_status()
                launched = started.json()
                client.put(f"/api/role/stories/{launched['story_id']}/settings", json={"paused": False}).raise_for_status()
                runtime = Runtime(db, config)
                runtime.submit(launched["player_id"], config.workspace_owner, MESSAGE, "schedule-fixed")
                with db.session() as s:
                    tasks = list(s.scalars(select(Task).where(Task.kind == "respond")))
                    assert len(tasks) == 1 and tasks[0].status == "queued"
                    identity.update(task_id=tasks[0].id, story_id=launched["story_id"], player_id=launched["player_id"])
                    identity["actors"] = launched["actors"]
                write(directory, "identity.json", identity)
        finally:
            await api.close()
            collect(db)
            seed_cost = totals["cost"]
            db.close()
        director.propose_plan = fixed_plan
        for pair in range(pairs):
            for width in ([1, 2] if pair % 2 == 0 else [2, 1]):
                if hashes != file_hashes():
                    raise RuntimeError("Frozen source changed; no further model request")
                name = "sceneweaver_schedule_" + uuid4().hex[:10] + "_test"
                create(name, seed)
                config = settings(name, width)
                db = Database(config)
                worker = Worker(db, config)
                requests, assignments = [], []
                original_chat = worker.provider.chat

                async def capture(task_id, role, messages, **kwargs):
                    if hashes != file_hashes():
                        raise RuntimeError("Frozen source changed before request; no further model request")
                    schema = kwargs.get("schema")
                    requests.append({"role": role, "operation": kwargs.get("operation"),
                        "messages_sha256": digest(messages), "schema_sha256": digest(schema.model_json_schema()) if schema else None,
                        "max_tokens": kwargs.get("max_tokens")})
                    return await original_chat(task_id, role, messages, **kwargs)

                worker.provider.chat = capture

                async def capture_assignment(*args, **kwargs):
                    assignment = await original_authorize(*args, **kwargs)
                    assignments.append({'actor_id': args[3].id, 'assignment': assignment, 'sha256': digest(assignment)})
                    return assignment

                directed_turn.authorize_assignment = capture_assignment
                try:
                    # Each copied queued task receives the same relative run
                    # allowance; waiting for previous arms is not player work.
                    with db.session.begin() as s:
                        queued = s.get(Task, identity["task_id"])
                        assert queued.status == "queued" and queued.calls == 0
                        queued.deadline = now() + timedelta(seconds=config.task_timeout)
                    task = worker.runtime.claim("schedule-probe")
                    assert task and task.id == identity["task_id"]
                    begun = time.monotonic()
                    await worker.run_task(task)
                    result = report(db, task, time.monotonic() - begun)
                    expected = {actor for alias, actor in identity["actors"].items() if alias != "player"}
                    with db.session() as s:
                        token_accounting = [{"usage_id": u.id, "role": u.role, "input_tokens": u.input_tokens,
                            "cache_tokens": u.cache_tokens, "output_tokens": u.output_tokens, "status": u.status}
                            for u in s.scalars(select(Usage).where(Usage.task_id == task.id))]
                    result.update(pair=pair + 1, preparation_concurrency=width, generation_requests=requests,
                                  assignments=assignments, token_accounting=token_accounting,
                                  new_cost_usd=result["database_cost_usd"] - seed_cost,
                                  fixed_workload_complete=workload_complete(result, expected))
                    name_out = f"pair-{pair + 1}-width-{width}.json"
                    write(directory, name_out, result)
                    rows.append({key: result[key] for key in ("pair", "preparation_concurrency", "status",
                        "stop_reason", "first_committed_text_s", "turn_s", "model_calls", "interaction_complete", "new_cost_usd")})
                    rows[-1].update(report=name_out, formal_count=len(result["segments"]),
                                    fixed_workload_complete=result["fixed_workload_complete"])
                    write(directory, "summary.json", rows)
                    print(json.dumps(rows[-1], ensure_ascii=False), flush=True)
                finally:
                    directed_turn.authorize_assignment = original_authorize
                    await worker.provider.close()
                    collect(db, seed_cost)
                    db.close()
        return totals
    finally:
        director.propose_plan, directed_turn.authorize_assignment = original_plan, original_authorize
        for name in reversed(names):
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        write(directory, "cleanup.json", {"deleted_test_databases": names})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-paid", action="store_true")
    parser.add_argument("--validate-fixture", action="store_true")
    parser.add_argument("--pairs", type=int, choices=[1, 2, 4], default=2)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-spend-usd", type=float, default=0.30)
    args = parser.parse_args()
    if args.validate_fixture:
        print(json.dumps({"recipe_sha256": digest(fixture()), "frontier": ["alan", "shen"], "dependent": "lin"}))
        return
    if not args.run_paid or args.output_dir is None or not 0 < args.max_spend_usd <= .30:
        parser.error("Paid opt-in, fresh .runtime directory and cap <=0.30 USD required")
    directory = args.output_dir.resolve()
    if not directory.is_relative_to(ROOT / ".runtime") or directory.exists():
        parser.error("A fresh directory inside .runtime is required")
    origin = Settings()
    if (origin.model_provider != "deepseek" or origin.api_base.rstrip("/") != "https://api.deepseek.com" or not origin.generation_key()
            or origin.dialogue_model != "deepseek-v4-flash" or origin.daily_budget_usd != 6):
        parser.error("Expected authorized official DeepSeek Flash and daily $6 cap")
    base, name = os.environ.get("TEST_DATABASE_URL",
        "postgresql+psycopg://continuum:continuum_local@127.0.0.1:55432/continuum_test").rsplit("/", 1)
    if not name.endswith("_test") or "@127.0.0.1:" not in base or not base.startswith("postgresql+psycopg://"):
        parser.error("Only the local explicitly named test server is allowed")
    directory.mkdir(parents=True)
    project = Database(origin)
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
    reservation = reserve_probe(project, origin, args.max_spend_usd)
    write(directory, "reservation.json", {"id": reservation, "cap_usd": args.max_spend_usd})
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        totals = asyncio.run(run_probe(origin, admin, base, directory, args.max_spend_usd, args.pairs))
        with project.session.begin() as s:
            row = s.get(Usage, reservation)
            row.cost, row.reserved = totals["cost"], totals["reserved"]
            row.status = "completed" if not row.reserved else "estimated_failed"
        write(directory, "settlement.json", {"id": reservation, **totals, "status": row.status})
    finally:
        # Unexpected failure leaves the batch reservation intact, never false zero.
        admin.dispose()
        project.close()


if __name__ == "__main__":
    main()
