"""Four paired comparisons of opt-in direct routing or single speech representation."""
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

CASES = [
    ("unknown_schedule", "你知道今晚渡船几点开吗？"),
    ("identity", "你叫什么，为什么会在这里？"),
    ("proposal", "如果还不清楚班次，你建议我现在做什么？"),
    ("willingness", "你愿意和我一起核对今晚渡船的安排吗？"),
]
FROZEN = [
    "examples/stories/ferry.story.json", "src/chatbot/config.py", "src/chatbot/directing.py",
    "src/chatbot/director_planning.py", "src/chatbot/director_state.py",
    "src/chatbot/npc/director.py", "src/chatbot/npc/direct_response.py", "src/chatbot/npc/directed_turn.py",
    "src/chatbot/npc/directed_scene.py", "src/chatbot/npc/planning.py", "src/chatbot/npc/performance.py",
    "src/chatbot/npc/combined_generation.py", "src/chatbot/npc/speech_packets.py",
    "src/chatbot/npc/speech_contract.py", "src/chatbot/npc/review.py", "src/chatbot/npc/grounded_assignment.py",
    "src/chatbot/npc/assignment.py", "src/chatbot/npc/context.py",
    "src/chatbot/source_authorization.py",
    "src/chatbot/role_policy.py", "src/chatbot/schemas.py", "src/chatbot/providers.py",
    "scripts/probe_response_architecture.py", "scripts/accept_directed_ferry_live.py",
    "scripts/probe_actor_pipeline.py", "scripts/probe_beat_reuse.py",
]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FROZEN}


def write(directory, name, value):
    (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")


def fixture(author_policy="source"):
    recipe = directed_recipe()
    node = recipe["narrative"]["nodes"][0]
    node["direction"] = {"objective": "回应玩家当前发言", "beats": [{
        "id": "reply", "title": "直接回应", "objective": "根据自己的所知回应玩家",
        "completion": "回应后交还玩家", "participants": ["captain"], "max_segments": 1,
        "handoff": "after_beat", "direct_response_actor": "captain",
        "source_response_tasks": True,
    }]}
    if author_policy == "constraints":
        node["direction"]["constraints"] = ["本段每条回应只用一至两句，不追加新的问题。"]
    return Recipe.model_validate(recipe).model_dump(mode="json")


def trial_schedule(repetitions, case_set="all"):
    """Two repetitions reverse arm order within every case, not just globally."""
    orders = [range(4), [1, 0, 3, 2]] if case_set == "all" else [[0, 1], [1, 0]]
    return [(repeat + 1, *CASES[index])
            for repeat in range(repetitions) for index in orders[repeat]]


async def fixed_reply(provider, task_id, direction, beat_id, **kwargs):
    beat = next(b for b in direction.beats if b.id == beat_id)
    events = json.loads(kwargs["public_context"])["events"]
    player = next(e["actor_id"] for e in events if e["event_id"] == kwargs["current_player_event_id"])
    if kwargs.get("recovery_reason") or kwargs.get("completed_jobs"):
        jobs = []
    else:
        jobs = [{"id": "reply", "actor": beat.direct_response_actor, "addressees": [player],
                 "grounded_response": {"event_id": kwargs["current_player_event_id"], "delivery": "brief"}}]
    return DirectorPlan(beat_id=beat_id, jobs=jobs, handoff=True,
                        reason="固定一次真实玩家输入回应，只比较台词表示。")


async def run(origin, admin, base, directory, cap, experiment, repetitions=1, case_set="all",
              planning="fixed", author_policy="source"):
    frozen = hashes()
    recipe = fixture(author_policy)
    totals = {"cost": 0.0, "reserved": 0.0}
    rows, names = [], []
    seed_cost = 0.0
    original_plan, original_authorize = director.propose_plan, directed_turn.authorize_assignment
    schedule = trial_schedule(repetitions, case_set)
    write(directory, "identity.json", {"experiment": experiment, "pairs": len(schedule), "cases": CASES,
        "repetitions_per_case": repetitions, "trial_schedule": schedule,
        "case_set": case_set,
        "file_hashes": frozen, "recipe_sha256": digest(recipe), "model": origin.dialogue_model,
        "provider": origin.model_provider, "cap_usd": cap, "tts": False, "acknowledgement": False,
        "generation": "separate" if experiment == "authorization" else "combined_context",
        "authorization": "semantic_vs_grounded_response" if experiment == "authorization" else "grounded_response",
        "review_format": origin.deepseek_review_format, "task_timeout_seconds": 240,
        "arms": 2 * len(schedule), "paid_director": experiment == "direct" or planning == "model",
        "planning": planning, "author_policy": author_policy, "default_switches_changed": False})

    def create(template=None):
        name = "sceneweaver_response_" + uuid4().hex[:10] + "_test"
        assert name.replace("_", "").isalnum() and name.endswith("_test")
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
        names.append(name)
        return name

    def settings(name, arm):
        piece = cap / (1 + 2 * len(schedule))
        return origin.model_copy(update={"product": "roleplay", "database_url": base + "/" + name,
            "data_dir": directory / "data", "daily_budget_usd": seed_cost + piece, "turn_budget_usd": piece,
            "max_calls_per_task": 24, "task_timeout": 240, "embedding_provider": "lexical",
            "tts_enabled": False, "acknowledgement_enabled": False, "openrouter_roles": {},
            "role_actor_generation": "separate" if experiment == "authorization" else "combined_context",
            "role_assignment_authorization": "semantic" if experiment == "authorization" and arm == "control" else "grounded_response",
            "role_response_review_binding": False, "role_player_action_preflight": False,
            "role_director_beat_reuse": False,
            "role_direct_response": experiment == "direct" and arm == "experimental",
            "role_speech_representation": "segments" if experiment == "packets" and arm == "experimental" else "legacy"})

    def collect(db, copied=0):
        with db.session() as s:
            totals["cost"] += float(s.scalar(select(func.coalesce(func.sum(Usage.cost), 0)))) - copied
            totals["reserved"] += float(s.scalar(select(func.coalesce(func.sum(Usage.reserved), 0))))
        write(directory, "accounting.json", totals)

    try:
        seed = create()
        config = settings(seed, "control")
        db = Database(config)
        api = Provider(db, config, Runtime(db, config))
        try:
            db.initialize()
            with TestClient(create_app(config, db, api)) as client:
                key = str(uuid4())
                client.put("/api/role/experiments/blueprints/" + key,
                    json={"expected_version": 0, "recipe": recipe}).raise_for_status()
                started = client.post("/api/role/experiments/start",
                    json={"request_id": key, "blueprint_id": key, "blueprint_version": 1})
                started.raise_for_status()
                launched = started.json()
                client.put(f"/api/role/stories/{launched['story_id']}/settings", json={"paused": False}).raise_for_status()
            write(directory, "seed.json", launched)
        finally:
            await api.close()
            collect(db)
            seed_cost = totals["cost"]
            db.close()
        if experiment in {"packets", "authorization"} and planning == "fixed":
            director.propose_plan = fixed_reply
        for pair, (repeat, case, message) in enumerate(schedule, 1):
            pair_seed = create(seed)
            config = settings(pair_seed, "control")
            db = Database(config)
            try:
                task = Runtime(db, config).submit(launched["player_id"], config.workspace_owner, message,
                    "response-pair-" + str(pair), addressees=[launched["actors"]["captain"]])
                task_id = task.id
            finally:
                db.close()
            for arm in (["control", "experimental"] if pair % 2 else ["experimental", "control"]):
                if hashes() != frozen:
                    raise RuntimeError("Frozen source changed; no further provider call")
                name = create(pair_seed)
                config = settings(name, arm)
                db = Database(config)
                worker = Worker(db, config)
                requests, assignments = [], []
                original_chat = worker.provider.chat

                async def capture(task_id, role, messages, **kwargs):
                    if hashes() != frozen:
                        raise RuntimeError("Frozen source changed before provider call")
                    schema = kwargs.get("schema")
                    requests.append({"role": role, "operation": kwargs.get("operation"),
                        "messages_sha256": digest(messages),
                        "schema_sha256": digest(schema.model_json_schema()) if schema else None,
                        "max_tokens": kwargs.get("max_tokens")})
                    return await original_chat(task_id, role, messages, **kwargs)

                async def capture_assignment(*args, **kwargs):
                    assignment = await original_authorize(*args, **kwargs)
                    assignments.append({"actor_id": args[3].id, "assignment": assignment, "sha256": digest(assignment)})
                    return assignment

                worker.provider.chat = capture
                directed_turn.authorize_assignment = capture_assignment
                try:
                    with db.session.begin() as s:
                        queued = s.get(Task, task_id)
                        assert queued.status == "queued" and queued.calls == 0
                        queued.deadline = now() + timedelta(seconds=config.task_timeout)
                    task = worker.runtime.claim("response-probe")
                    assert task and task.id == task_id
                    begun = time.monotonic()
                    await worker.run_task(task)
                    result = report(db, task, time.monotonic() - begun)
                    result.update(experiment=experiment, pair=pair, repeat=repeat, case=case, message=message, arm=arm,
                        generation_requests=requests, assignments=assignments,
                        new_cost_usd=result["database_cost_usd"] - seed_cost,
                        response_workload_complete=bool(result["status"] == "completed"
                            and result["stop_reason"] != "safety_rejected" and len(result["segments"]) == 1
                            and result["segments"][0]["actor_id"] == launched["actors"]["captain"]),
                        output_tokens=sum(u["output_tokens"] for u in result["model_usage"]),
                        dialogue_output_tokens=sum(u["output_tokens"] for u in result["model_usage"] if u["role"] == "dialogue"))
                    out = f"pair-{pair}-{arm}.json"
                    write(directory, out, result)
                    row = {k: result[k] for k in ("pair", "repeat", "case", "arm", "status", "stop_reason",
                        "first_committed_text_s", "turn_s", "model_calls", "new_cost_usd",
                        "response_workload_complete", "output_tokens", "dialogue_output_tokens")}
                    row["report"] = out
                    rows.append(row)
                    write(directory, "summary.json", rows)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                finally:
                    directed_turn.authorize_assignment = original_authorize
                    await worker.provider.close()
                    collect(db, seed_cost)
                    db.close()
        write(directory, "source-verification.json", {"frozen_source_still_matches": hashes() == frozen})
        return totals
    finally:
        director.propose_plan, directed_turn.authorize_assignment = original_plan, original_authorize
        deleted = []
        for name in reversed(names):
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            deleted.append(name)
        write(directory, "cleanup.json", {"created_test_databases": names, "deleted_test_databases": deleted})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", choices=["direct", "packets", "authorization"], required=True)
    parser.add_argument("--repetitions", type=int, choices=[1, 2], default=1)
    parser.add_argument("--case-set", choices=["all", "two"], default="all")
    parser.add_argument("--planning", choices=["fixed", "model"], default="fixed")
    parser.add_argument("--author-policy", choices=["source", "constraints"], default="source")
    parser.add_argument("--run-paid", action="store_true")
    parser.add_argument("--validate-fixture", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--max-spend-usd", type=float, default=.30)
    args = parser.parse_args()
    if args.validate_fixture:
        print(json.dumps({"recipe_sha256": digest(fixture(args.author_policy)), "pairs": len(trial_schedule(args.repetitions, args.case_set)),
                          "trial_schedule": trial_schedule(args.repetitions, args.case_set)}))
        return
    if args.author_policy == "constraints" and args.planning != "model":
        parser.error("Author constraints coverage requires the real director")
    if args.experiment == "direct" and args.repetitions != 1:
        parser.error("Additional repetitions are only registered for task-equivalent packets, not confounded direct routing")
    if args.case_set != "all" and args.experiment != "authorization":
        parser.error("The two-case repeated fixture is registered only for authorization")
    if not args.run_paid or args.output_dir is None or not 0 < args.max_spend_usd <= .30:
        parser.error("Paid opt-in, fresh .runtime directory and cap <=0.30 USD required")
    directory = args.output_dir.resolve()
    if not directory.is_relative_to(ROOT / ".runtime") or directory.exists():
        parser.error("Fresh output directory inside .runtime required")
    origin = Settings()
    if (origin.model_provider != "deepseek" or origin.api_base.rstrip("/") != "https://api.deepseek.com"
            or not origin.generation_key() or origin.dialogue_model != "deepseek-v4-flash"
            or origin.daily_budget_usd != 6):
        parser.error("Expected authorized official DeepSeek Flash and project daily $6")
    base, name = os.environ.get("TEST_DATABASE_URL",
        "postgresql+psycopg://continuum:continuum_local@127.0.0.1:55432/continuum_test").rsplit("/", 1)
    if not name.endswith("_test") or "@127.0.0.1:" not in base or not base.startswith("postgresql+psycopg://"):
        parser.error("Only the explicitly named local test server is allowed")
    directory.mkdir(parents=True)
    project = Database(origin)
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
    reservation = reserve_probe(project, origin, args.max_spend_usd)
    write(directory, "reservation.json", {"id": reservation, "cap_usd": args.max_spend_usd})
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        totals = asyncio.run(run(origin, admin, base, directory, args.max_spend_usd, args.experiment,
                                args.repetitions, args.case_set, args.planning, args.author_policy))
        with project.session.begin() as s:
            usage = s.get(Usage, reservation)
            usage.cost, usage.reserved = totals["cost"], totals["reserved"]
            usage.status = "completed" if not usage.reserved else "estimated_failed"
            final_status = usage.status
        write(directory, "settlement.json", {"id": reservation, **totals, "status": final_status})
    finally:
        # Unknown spend on exceptional exit stays reserved, never silently set to zero.
        admin.dispose()
        project.close()


if __name__ == "__main__":
    main()
