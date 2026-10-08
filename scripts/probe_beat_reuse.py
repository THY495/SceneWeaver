"""Official-model two-turn probe, cloning the same committed opening for each arm.

The opening direction is fixed to elicit a player question; actors, review and
completion remain real. The second turn compares ordinary direction to reuse.
Only random, local *_test databases are created/dropped. Costs are reserved in
the project ledger before API calls. No TTS, browser or packaged-game claim.
"""

import argparse
import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

from accept_directed_ferry_live import ROOT, directed_recipe
from fastapi.testclient import TestClient
from probe_actor_pipeline import reserve_probe
from sqlalchemy import create_engine, func, select, text

from chatbot.api import create_app
from chatbot.config import Settings
from chatbot.directing import DirectorPlan
from chatbot.npc import director
from chatbot.npc.beat_reuse import frame_key
from chatbot.providers import Provider, operation_cache_name
from chatbot.runtime import Runtime
from chatbot.storage import Database, Operation, SceneRecord, Story, Task, Usage
from chatbot.story_blueprints import Recipe
from chatbot.worker import Worker


def fixture(*, exercise_routes=False):
    recipe = directed_recipe()
    beat = recipe["narrative"]["nodes"][0]["direction"]["beats"][0]
    beat.update(handoff="on_question", max_segments=5,
                objective="阿岚先问玩家是否先聊公告；玩家回答后，沈舟回应阿岚，林澈回应沈舟。",
                completion="玩家已经回应阿岚，三名角色都表达过，且已发生NPC之间的回应。")
    if exercise_routes:
        conditions = {
            "opening-notice": "玩家明确决定自己去阅读公告栏；只讨论公告或尚未决定不满足。",
            "opening-weather": "玩家明确决定自己去窗边观察天气；只讨论天气或尚未决定不满足。",
            "notice-merge": "玩家明确决定返回人群，与大家继续交谈。",
            "weather-merge": "玩家明确决定返回人群，与大家继续交谈。",
            "merge-wait": "玩家明确决定留在候亭等待消息。",
            "merge-plan": "玩家明确决定考虑其他安排，暂时不在候亭等消息。",
        }
        for edge in recipe["narrative"]["edges"]:
            edge.update(trigger="ai", condition=conditions[edge["id"]])
    return Recipe.model_validate(recipe).model_dump(mode="json")


async def opening_plan(provider, task_id, direction, beat_id, *, typed_responses=False, **kwargs):
    cast = json.loads(kwargs["public_context"])["cast"]
    names = {person["name"]: person["id"] for person in cast}
    player = next(person["id"] for person in cast if person["name"] not in {"阿岚", "沈舟", "林澈"})
    jobs = [
        {"id": "ask-player", "actor": names["阿岚"], "addressees": [player],
         "objective": "只询问玩家是否先谈公告，不回答公告的内容。",
         "delivery_constraints": ["一句简短问题；等待玩家回答。"]},
        {"id": "shen-answer", "actor": names["沈舟"], "addressees": [names["阿岚"]],
         "objective": "回应阿岚和玩家已经说过的话，表达自己已知和未知的内容。", "depends_on": ["ask-player"],
         "delivery_constraints": ["简短一至两句；不新增需要玩家回答的问题。"]},
        {"id": "lin-answer", "actor": names["林澈"], "addressees": [names["沈舟"]],
         "objective": "回应沈舟实际说过的话，表达自己的看法，自然结束本段。", "depends_on": ["shen-answer"],
         "delivery_constraints": ["简短一至两句；不新增需要答复的问题。"]},
    ]
    if typed_responses:
        for job in jobs[1:]:
            job.pop("objective")
            job.pop("delivery_constraints")
            job["grounded_response"] = {"job_id": job["depends_on"][0], "delivery": "brief"}
    return DirectorPlan(beat_id=beat_id, reason="固定首轮提问，以便复制完全相同的已提交前情。", jobs=jobs)


def route_steps(enabled):
    """Different story branches follow the comparable same-input second turn."""
    if enabled:
        return [("notice", "我决定先去阅读公告栏，暂时不看窗外天气。我自己过去看看。"),
                ("merge", "我现在返回人群，与大家继续交谈。"),
                ("wait", "我决定留在候亭，等待消息。")]
    return [("weather", "我决定先去窗边看看外面的天气；公告栏暂时不读。我自己过去看看。"),
            ("merge", "我现在返回人群，与大家继续交谈。"),
            ("plan", "我决定考虑其他安排，暂时不在候亭等消息。")]


def continue_assessed_route(client, story_id):
    """Use the player's normal automatic continuation endpoint, never author choose."""
    path = f"/api/role/experiments/stories/{story_id}/narrative"
    response = client.get(path)
    response.raise_for_status()
    frame = response.json()
    if frame.get("auto_ready"):
        continued = client.post(path + "/auto-continue",
                                json={"through_seq": frame["auto_ready"]["through_seq"]})
        continued.raise_for_status()
        return continued.json()
    return frame


def apply_player_continuation(config, db, story_id):
    # FastAPI shutdown owns and closes its injected provider. The short-lived
    # player API must never take ownership of the still-running Worker's client.
    api_provider = Provider(db, config, Runtime(db, config))
    with TestClient(create_app(config, db, api_provider)) as client:
        return continue_assessed_route(client, story_id)


def source_authorizations(session, task, operations):
    """Expose real committed NPC links, without claiming semantic relevance."""
    ledgers = {op.payload.get('revision'): op.payload for op in operations
        if ':director-ledger' in op.id and isinstance(op.payload, dict)}
    rows = []
    for op in operations:
        proof = op.payload
        if proof.get('assignment_authorization') != 'grounded_response':
            continue
        ledger = ledgers.get(proof.get('director_revision'), {})
        job = next((row for row in ledger.get('plan', {}).get('jobs', [])
            if row['id'] == proof.get('job_id')), {})
        candidate = ledger.get('candidates', {}).get(proof.get('job_id'), {})
        source = session.get(SceneRecord, proof.get('source_event_id'))
        reply = session.get(SceneRecord, candidate['event_id']) if candidate.get('event_id') else None

        def committed(event):
            if not event or not event.task_id or event.kind != 'speech' or event.payload.get('acknowledgement'):
                return False
            return any(row.payload.get('event_id') == event.id
                and row.payload.get('actor_id') == event.actor_id for row in session.scalars(
                    select(Operation).where(Operation.task_id == event.task_id,
                        Operation.id.startswith(event.task_id + ':committed:'))))

        source_job = ledger.get('candidates', {}).get(proof.get('source_job_id'), {})
        dependency_matches = not proof.get('source_job_id') or (
            proof['source_job_id'] in job.get('depends_on', [])
            and source_job.get('status') == 'committed'
            and source_job.get('event_id') == proof.get('source_event_id')
            and source_job.get('revision') == proof.get('source_candidate_revision'))
        verified = bool(source and reply and committed(source) and committed(reply)
            and candidate.get('status') == 'committed' and job.get('actor') == proof.get('actor_id') == reply.actor_id
            and source.story_id == reply.story_id and source.actor_id != reply.actor_id
            and source.actor_id != task.session_id and reply.actor_id != task.session_id
            and source.created_at < reply.created_at and source.actor_id in reply.addressees
            and (not proof.get('source_producer_actor') or proof['source_producer_actor'] == source.actor_id)
            and dependency_matches)
        rows.append({'authorization_operation': op.id, 'proof': proof,
            'purpose': (job.get('grounded_response') or {}).get('purpose', 'respond'),
            'source_event_id': source.id if source else None,
            'source_task_id': source.task_id if source else None,
            'source_actor_id': source.actor_id if source else None,
            'source_text': source.text if source else None,
            'response_event_id': reply.id if reply else None,
            'response_actor_id': reply.actor_id if reply else None,
            'response_text': reply.text if reply else None,
            'verified_committed_link': verified,
            'semantic_relevance': 'not assessed by structural evidence'})
    return rows


def report(db, task, duration):
    from chatbot.event_projection import requests_for

    with db.session() as s:
        finished = s.get(Task, task.id)
        result = finished.result or {}
        usage = list(s.scalars(select(Usage).where(Usage.task_id == task.id)))
        operations = list(s.scalars(select(Operation).where(Operation.task_id == task.id)))
        timings = [op.payload["timing_v1"] for op in operations if "timing_v1" in op.payload]
        all_speech = list(s.scalars(select(SceneRecord).where(SceneRecord.task_id == task.id,
                            SceneRecord.kind == "speech", SceneRecord.actor_id != task.session_id)
                            .order_by(SceneRecord.seq)))
        records = [row for row in all_speech if not row.payload.get("acknowledgement")]
        intentions = {segment["event_id"]: segment.get("intent", {}) for segment in result.get("segments", [])}
        player = s.get(SceneRecord, finished.payload["event_id"])
        story = s.get(Story, player.story_id)
        # Timing clock begins in Worker, excluding submit and claim, just as the
        # previous actor probe. Do not count the player's submitted speech.
        from datetime import datetime

        starts = [datetime.fromisoformat(t["started_at"]) for t in timings if t.get("started_at")]
        first = round((records[0].created_at - min(starts)).total_seconds(), 3) if records and starts else None
        return {"task_id": task.id, "status": finished.status, "error": finished.error,
                "stop_reason": result.get("stop_reason"), "turn_s": round(duration, 3),
                "first_committed_text_s": first, "cost_usd": sum(u.cost + u.reserved for u in usage),
                "database_cost_usd": float(s.scalar(select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)))),
                "model_calls": dict(Counter(u.role for u in usage)),
                "model_usage": [{"role": u.role, "model": u.model, "duration_ms": u.duration_ms,
                    "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                    "status": u.status, "reserved_usd": u.reserved, "cost_usd": u.cost} for u in usage],
                "candidate_audit": [{"operation": op.id, "cache_operation": operation_cache_name(op),
                    "value": op.payload["value"]} for op in operations
                    if isinstance(op.payload.get("value"), dict)
                    and any(marker in operation_cache_name(op) for marker in ("actor-turn-", ":answer:", ":review:"))],
                "source_authorizations": source_authorizations(s, finished, operations),
                "segments": [{"event_id": r.id, "actor": r.actor_name, "text": r.text,
                    "actor_id": r.actor_id, "addressees": r.addressees, "intent": intentions.get(r.id, {}),
                    "committed_at": r.created_at.isoformat()} for r in records],
                "acknowledgements": [{"event_id": r.id, "actor": r.actor_name, "text": r.text,
                    "committed_at": r.created_at.isoformat()} for r in all_speech if r.payload.get("acknowledgement")],
                "interaction_complete": result.get("interaction_complete"),
                "player_questions": [request for request in requests_for(s, task.session_id)
                    if request["task_id"] == task.id and request["target_id"] == task.session_id
                    and request["status"] in {"open", "partial", "defer"}],
                "pending_reply_ids": result.get("pending_reply_ids", []), "timings": timings,
                "narrative": story.settings.get("narrative", {}),
                "auto_route": story.settings.get("auto_route", {}),
                "route_operations": [{"id": op.id, "payload": op.payload} for op in operations
                                     if op.id.startswith("scene-routing-audit:")],
                "reuse_link": next((o.payload for o in operations if o.id.endswith(":beat-reuse-link")), None),
                "director_ledger": next((o.payload for o in operations if o.id.endswith(":director-ledger")), None)}


async def run_probe(origin, admin, base, directory, cap, pairs, *, typed_responses=False, exercise_routes=False):
    from functools import partial

    original_plan = director.propose_plan
    names = []
    total = 0
    summary = []
    seed = "sceneweaver_beat_" + uuid4().hex[:10] + "_test"
    seed_cost = 0
    piece = cap / (1 + 2 * pairs * (4 if exercise_routes else 1))

    def create(name, template=None):
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")))
        names.append(name)

    def settings(name, enabled):
        configured = origin.model_copy(update={"product": "roleplay", "database_url": base + "/" + name,
            "data_dir": directory / "data", "daily_budget_usd": seed_cost + piece * (4 if exercise_routes else 1),
            "turn_budget_usd": piece,
            "max_calls_per_task": 48, "embedding_provider": "lexical", "tts_enabled": False,
            "acknowledgement_enabled": False, "openrouter_roles": {},
            "role_actor_generation": "combined_context", "role_director_beat_reuse": enabled})
        if typed_responses:
            configured.role_assignment_authorization = "grounded_response"
        return configured

    try:
        create(seed)
        config = settings(seed, True)
        db = Database(config)
        db.initialize()
        worker = Worker(db, config)
        api = Provider(db, config, Runtime(db, config))
        try:
            with TestClient(create_app(config, db, api)) as client:
                blueprint = str(uuid4())
                saved = client.put("/api/role/experiments/blueprints/" + blueprint,
                    json={"expected_version": 0, "recipe": fixture(exercise_routes=exercise_routes)})
                saved.raise_for_status()
                started = client.post("/api/role/experiments/start", json={"request_id": blueprint,
                    "blueprint_id": blueprint, "blueprint_version": 1})
                if not started.is_success:
                    (directory / "start-error.json").write_text(json.dumps({"status": started.status_code,
                        "detail": started.json()}, ensure_ascii=False, indent=2), encoding="utf-8")
                started.raise_for_status()
                story_id, player = started.json()["story_id"], started.json()["player_id"]
                client.put(f"/api/role/stories/{story_id}/settings", json={"paused": False}).raise_for_status()
                director.propose_plan = partial(opening_plan, typed_responses=typed_responses)
                worker.runtime.submit(player, config.workspace_owner, "阿岚，先问问我想谈什么，再让你们三人交流。", "opening")
                task = worker.runtime.claim("beat-probe")
                begun = time.monotonic()
                await worker.run_task(task)
                opening = report(db, task, time.monotonic() - begun)
                seed_cost = opening["database_cost_usd"]
                total += seed_cost
                (directory / "opening.json").write_text(json.dumps(opening, ensure_ascii=False, indent=2), encoding="utf-8")
                with db.session() as s:
                    frame = s.get(Operation, frame_key(story_id))
                    if not frame or frame.payload.get("state") != "ready" or opening["status"] != "completed":
                        raise RuntimeError("Real opening did not produce a reusable unfinished beat; inspect opening.json")
        finally:
            director.propose_plan = original_plan
            await worker.provider.close()
            await api.close()
            db.close()
        for pair in range(pairs):
            for enabled in ([False, True] if pair % 2 == 0 else [True, False]):
                name = "sceneweaver_beat_" + uuid4().hex[:10] + "_test"
                create(name, seed)
                config = settings(name, enabled)
                db = Database(config)
                worker = Worker(db, config)
                try:
                    worker.runtime.submit(player, config.workspace_owner, "是的，先谈公告。", "response-" + uuid4().hex)
                    task = worker.runtime.claim("beat-probe")
                    begun = time.monotonic()
                    await worker.run_task(task)
                    result = report(db, task, time.monotonic() - begun)
                    result["all_new_cost_usd"] = result["database_cost_usd"] - seed_cost
                    total += result["all_new_cost_usd"]
                    result.update(pair=pair + 1, beat_reuse_enabled=enabled)
                    file = f"pair-{pair + 1}-{'reuse' if enabled else 'fresh'}.json"
                    (directory / file).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    summary.append({key: result[key] for key in ("pair", "beat_reuse_enabled", "status", "stop_reason",
                        "first_committed_text_s", "turn_s", "cost_usd", "model_calls", "interaction_complete")})
                    summary[-1].update(reuse_hit=bool(result["reuse_link"]), report=file)
                    print(json.dumps(summary[-1], ensure_ascii=False), flush=True)
                    (directory / "summary.json").write_text(json.dumps({"opening_cost_usd": opening["cost_usd"],
                        "total_cost_usd": total, "comparisons": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
                    if exercise_routes:
                        routes = []
                        for index, (expected_node, message) in enumerate(route_steps(enabled)):
                            worker.runtime.submit(player, config.workspace_owner, message, "route-" + uuid4().hex)
                            route_task = worker.runtime.claim("beat-probe")
                            if not route_task:
                                raise RuntimeError("No routing task claimed")
                            begun = time.monotonic()
                            await worker.run_task(route_task)
                            route = report(db, route_task, time.monotonic() - begun)
                            route["player_continuation"] = apply_player_continuation(config, db, story_id)
                            with db.session() as s:
                                route["narrative_after_continuation"] = s.get(Story, story_id).settings.get("narrative", {})
                            report_after = route["database_cost_usd"]
                            total += report_after - result["database_cost_usd"]
                            result["database_cost_usd"] = report_after
                            frame = route["player_continuation"]
                            reached = route["status"] == "completed" and route["narrative_after_continuation"].get("node") == expected_node
                            ending = expected_node in {"wait", "plan"}
                            route.update(expected_node=expected_node, player_input=message,
                                reached_expected=reached,
                                ending_verified=(bool(reached and frame.get("ending") and frame.get("finished_at")
                                    and frame.get("execution", {}).get("phase") not in {"opening", "video"}
                                    and not route["pending_reply_ids"]) if ending else None))
                            routes.append(route)
                            route_file = f"pair-{pair + 1}-{'reuse' if enabled else 'fresh'}-route-{index + 1}.json"
                            (directory / route_file).write_text(json.dumps(route, ensure_ascii=False, indent=2), encoding="utf-8")
                            print(json.dumps({"report": route_file, "status": route["status"],
                                "expected": expected_node, "actual": route["narrative_after_continuation"].get("node"),
                                "reached_expected": route["reached_expected"]}, ensure_ascii=False), flush=True)
                            if not route["reached_expected"] or (ending and not route["ending_verified"]):
                                break
                        summary[-1]["routes"] = [{key: route[key] for key in
                            ("task_id", "expected_node", "reached_expected", "ending_verified", "status", "turn_s", "cost_usd")}
                            for route in routes]
                        (directory / "summary.json").write_text(json.dumps({
                            "scope": "Official model source API/Worker; fixed opening director; no TTS/browser/native game",
                            "typed_responses": typed_responses, "exercise_routes": exercise_routes,
                            "opening_cost_usd": opening["cost_usd"], "total_cost_usd": total,
                            "comparisons": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
                finally:
                    await worker.provider.close()
                    db.close()
        return total
    finally:
        director.propose_plan = original_plan
        for name in reversed(names):
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-paid", action="store_true")
    parser.add_argument("--pairs", type=int, choices=[1, 2], default=1)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-spend-usd", type=float, default=0.6)
    parser.add_argument("--typed-responses", action="store_true")
    parser.add_argument("--exercise-routes", action="store_true")
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    if not args.run_paid or not directory.is_relative_to(ROOT / ".runtime") or directory.exists():
        parser.error("Explicit paid opt-in and a fresh .runtime directory are required")
    if not 0 < args.max_spend_usd <= 0.8:
        parser.error("Bounded probe cap must be <=0.80 USD")
    if args.exercise_routes and not args.typed_responses:
        parser.error("Combined routing validation requires typed responses")
    origin = Settings()
    if origin.api_base.rstrip("/") != "https://api.deepseek.com" or not origin.generation_key():
        parser.error("Only the previously authorized official DeepSeek API is allowed")
    if origin.daily_budget_usd != 6:
        parser.error("Expected the authorized daily 6 USD budget")
    base, name = os.environ.get("TEST_DATABASE_URL",
        "postgresql+psycopg://continuum:continuum_local@127.0.0.1:55432/continuum_test").rsplit("/", 1)
    if not name.endswith("_test") or "@127.0.0.1:" not in base or not base.startswith("postgresql+psycopg://"):
        parser.error("Only a local *_test server is allowed")
    directory.mkdir(parents=True)
    project = Database(origin)
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5})
    reservation = reserve_probe(project, origin, args.max_spend_usd)
    (directory / "reservation.json").write_text(json.dumps({"id": reservation,
        "reserved_usd": args.max_spend_usd}), encoding="utf-8")
    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        cost = asyncio.run(run_probe(origin, admin, base, directory, args.max_spend_usd, args.pairs,
                                   typed_responses=args.typed_responses, exercise_routes=args.exercise_routes))
        with project.session.begin() as s:
            row = s.get(Usage, reservation)
            row.cost, row.reserved, row.status = cost, 0, "completed"
    finally:
        # On uncertain failures preserve the reservation; do not report zero cost.
        admin.dispose()
        project.close()


if __name__ == "__main__":
    main()
