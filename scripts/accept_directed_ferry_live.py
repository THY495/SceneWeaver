"""Probe a deliberately directed three-NPC derivative of the synthetic ferry story.

Uses a fresh local *_test database and official DeepSeek. This is a source API
and Worker check, not a browser, voice, frozen-package or native-game check.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from chatbot.api import create_app
from chatbot.config import Settings
from chatbot.event_projection import requests_for
from chatbot.observability.usage import cache_reporting
from chatbot.providers import Provider, operation_cache_name
from chatbot.runtime import Runtime
from chatbot.storage import Conversation, Database, Operation, SceneRecord, Task, Usage
from chatbot.story_blueprints import Recipe
from chatbot.worker import Worker

if __package__:
    from .acceptance_scope import (
        execution_identity,
        ledger,
        reserve_acceptance_budget,
        settle_acceptance_budget,
    )
else:
    from acceptance_scope import (
        execution_identity,
        ledger,
        reserve_acceptance_budget,
        settle_acceptance_budget,
    )

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "examples/stories/ferry.story.json"
MESSAGE = (
    "阿岚、沈舟、林澈，请你们三人先彼此谈谈对今晚渡船的看法；各自说清知道什么、不知道什么，最后再让我接话。"
)


def directed_recipe():
    recipe = json.loads(SOURCE.read_text(encoding="utf-8"))["recipe"]
    people = [person["alias"] for person in recipe["people"]]
    if len(people) != 3 or len(set(people)) != 3:
        raise ValueError("Expected exactly the three synthetic ferry NPCs")
    entry = next(node for node in recipe["narrative"]["nodes"] if node["id"] == "opening")
    if entry.get("direction") is not None:
        raise ValueError("Base ferry story acquired a director; review this derivative")
    entry["observers"] = [recipe["player"]["alias"], *people]
    entry["participants"] = people
    entry["direction"] = {
        "version": 1,
        "objective": "三名在场角色彼此交换看法，各自说清知道和不知道的事，再交还玩家。",
        "constraints": ["不把猜测当成船班通知；不要代替玩家作决定。"],
        "beats": [
            {
                "id": "three-voices",
                "title": "三人交流",
                "objective": "阿岚、沈舟、林澈各自表达立场，至少有一次角色之间的回应。",
                "participants": people,
                "completion": "三人都已表达，并且没有未获回应的 NPC 问题；交还玩家。",
                "handoff": "after_beat",
                "max_segments": 5,
            }
        ],
    }
    # This probe isolates direction. The approved full visual/item story remains
    # untouched and is tested separately in the independent game browser.
    recipe["image_assets"] = []
    recipe["video_assets"] = []
    recipe["item_world"] = None
    for person in recipe["people"]:
        person["presentation"] = {"version": 1, "base_sprite": None, "default_outfit": None, "outfits": []}
    for node in recipe["narrative"]["nodes"]:
        node["background"] = None
        node["stage"] = None
        node["objects"] = []
    return Recipe.model_validate(recipe).model_dump(mode="json")


async def run(config, db, output):
    worker = Worker(db, config)
    api_provider = Provider(db, config, Runtime(db, config))
    report = {
        "scope": "Approved synthetic ferry derivative with authored three-NPC direction; official DeepSeek; source API/Worker; no browser or TTS",
        "message": MESSAGE,
        "fixture": {"source": str(SOURCE.relative_to(ROOT)), "node": "opening", "max_segments": 5},
        **execution_identity(config),
    }
    try:
        with TestClient(create_app(config, db, api_provider)) as client:
            blueprint = str(uuid4())
            base = "/api/role/experiments"
            saved = client.put(
                base + "/blueprints/" + blueprint,
                json={"expected_version": 0, "recipe": directed_recipe()},
            )
            saved.raise_for_status()
            started = client.post(
                base + "/start",
                json={"request_id": blueprint, "blueprint_id": blueprint, "blueprint_version": 1},
            )
            started.raise_for_status()
            run = started.json()
            story_id, player_id = run["story_id"], run["player_id"]
            expected_actors = {actor_id for alias, actor_id in run["actors"].items() if alias != "player"}
            report.update(story_id=story_id, player_id=player_id, expected_actor_ids=sorted(expected_actors))
            client.put(f"/api/role/stories/{story_id}/settings", json={"paused": False}).raise_for_status()
            worker.runtime.submit(
                player_id, config.workspace_owner, MESSAGE, "directed-ferry-" + uuid4().hex[:12]
            )
            task = worker.runtime.claim("directed-ferry-live")
            if not task:
                raise RuntimeError("No player task was claimed")
            report["task_id"] = task.id
            begun = time.monotonic()
            await worker.run_task(task)
            report["turn_seconds"] = round(time.monotonic() - begun, 2)
            with db.session() as s:
                finished = s.get(Task, task.id)
                result = finished.result or {}
                operations = list(s.scalars(select(Operation).where(Operation.task_id == task.id)))
                plans = [op.payload.get("plan", {}) for op in operations if ":director-ledger" in op.id]
                usage = list(s.scalars(select(Usage).where(Usage.task_id == task.id)))
                cache_reports = cache_reporting(s, usage)
                names = {
                    aid: s.get(Conversation, aid).current_state["profile"]["name"] for aid in expected_actors
                }
                report["task"] = {
                    "status": finished.status,
                    "error": finished.error,
                    "stop_reason": result.get("stop_reason"),
                    "interaction_complete": result.get("interaction_complete"),
                    "pending_reply_ids": result.get("pending_reply_ids", []),
                    "speaker_names": [names.get(aid, aid) for aid in result.get("actor_ids", [])],
                    "segments": [
                        {
                            "speaker": names.get(segment["actor_id"], segment["actor_id"]),
                            "text": segment["text"],
                            "addressee": names.get(segment.get("intent", {}).get("addressee_id"), "player"),
                            "reply_to": segment.get("intent", {}).get("reply_to"),
                            "expects_reply": segment.get("intent", {}).get("expects_reply"),
                        }
                        for segment in result.get("segments", [])
                    ],
                }
                report["committed_speech"] = [
                    {"speaker": row.actor_name, "text": row.text, "event_id": row.id}
                    for row in s.scalars(
                        select(SceneRecord)
                        .where(SceneRecord.task_id == task.id, SceneRecord.kind == "speech")
                        .order_by(SceneRecord.seq)
                    )
                    if row.actor_id in expected_actors
                ]
                report["director_plans"] = [
                    {
                        "beat_id": plan.get("beat_id"),
                        "jobs": [
                            {
                                "id": job["id"],
                                "actor": names.get(job["actor"], job["actor"]),
                                "objective": job["objective"],
                                "depends_on": job.get("depends_on", []),
                            }
                            for job in plan.get("jobs", [])
                        ],
                    }
                    for plan in plans
                ]
                report["open_npc_questions"] = [
                    {
                        "question_id": request["question_id"],
                        "asker": names.get(request["asker_id"], request["asker_id"]),
                        "target": names.get(request["target_id"], request["target_id"]),
                        "question": request["question"],
                    }
                    for request in requests_for(s, player_id)
                    if request["task_id"] == task.id
                    and request["target_id"] in expected_actors
                    and request["status"] in {"open", "partial"}
                ]
                report["model_usage"] = [
                    {
                        "role": row.role,
                        "duration_ms": row.duration_ms,
                        "cost_usd": row.cost,
                        "input_tokens": row.input_tokens,
                        "output_tokens": row.output_tokens,
                        "cache_tokens": row.cache_tokens,
                        "cache_tokens_reported": cache_reports[row.id],
                    }
                    for row in usage
                ]
                report["intent_validation"] = [
                    {"marks": span.get("marks", {}), "duration_ms": span.get("duration_ms")}
                    for operation in operations
                    for span in operation.payload.get("timing_v1", {}).get("spans", [])
                    if span.get("name") == "npc.intent_validation"
                ]
                report["intent_correction_operations"] = [
                    operation.id
                    for operation in operations
                    if ":intent:" in operation.id and operation.id.endswith(":1")
                ]
                report["assignment_validation"] = [
                    {
                        "name": span.get("name"),
                        "marks": span.get("marks", {}),
                        "duration_ms": span.get("duration_ms"),
                        "attributes": {
                            key: value
                            for key, value in span.get("attributes", {}).items()
                            if key
                            in {
                                "actor_id",
                                "job_id",
                                "assignment_provider_accepted",
                                "assignment_evidence_invalid_count",
                                "assignment_reply_capacity_exhausted",
                                "assignment_evidence_missing_fields_count",
                                "assignment_evidence_extent_invalid_count",
                                "assignment_evidence_source_class_invalid_count",
                                "assignment_evidence_expression_source_invalid_count",
                                "assignment_evidence_quote_missing_count",
                                "assignment_evidence_quote_not_found_count",
                            }
                        },
                    }
                    for operation in operations
                    for span in operation.payload.get("timing_v1", {}).get("spans", [])
                    if span.get("name") in {"npc.assignment", "npc.assignment_review_recheck"}
                ]
                report["validation_spans"] = [
                    {
                        "name": span["name"],
                        "duration_ms": span.get("duration_ms"),
                        "marks": span.get("marks", {}),
                    }
                    for operation in operations
                    for span in operation.payload.get("timing_v1", {}).get("spans", [])
                    if span.get("name")
                    in {
                        "npc.claim_validation",
                        "npc.semantic_review_result",
                        "npc.local_fact_pruning",
                        "npc.review_pruning",
                        "npc.purpose_recovery",
                        "npc.output_limit_retry",
                    }
                ]
                report["review_verdicts"] = [
                    {
                        "operation": operation.id,
                        "cache_operation": operation_cache_name(operation),
                        "verdict_scope": "provider_only_not_runtime",
                        "accepted": operation.payload["value"].get("accepted"),
                        "issue_count": len(operation.payload["value"].get("issues", [])),
                        "checks": [
                            {key: check.get(key) for key in ("supported", "reason_code")}
                            for check in operation.payload["value"].get("checks", [])
                        ],
                    }
                    for operation in operations
                    if (
                        ":review:" in operation_cache_name(operation)
                        or ":assignment-v9:" in operation_cache_name(operation)
                        or ":assignment-v9-format-repair:" in operation_cache_name(operation)
                        or ":assignment-v9-reference-recheck-v1:" in operation_cache_name(operation)
                    )
                    and isinstance(operation.payload.get("value"), dict)
                ]
                report["cost_usd"] = round(sum(row.cost for row in usage), 8)
                report["passed"] = (
                    finished.status == "completed"
                    and bool(plans)
                    and set(result.get("actor_ids", [])) == expected_actors
                    and result.get("interaction_complete") is True
                    and not result.get("pending_reply_ids")
                    and any(
                        segment.get("intent", {}).get("addressee_id") in expected_actors
                        and segment.get("intent", {}).get("addressee_id") != segment["actor_id"]
                        for segment in result.get("segments", [])
                    )
                )
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        await worker.provider.close()
        await api_provider.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--validate-fixture", action="store_true", help="Check the derivative without model calls"
    )
    parser.add_argument("--require-default-execution", action="store_true")
    args = parser.parse_args()
    recipe = directed_recipe()
    if args.validate_fixture:
        print(json.dumps({"people": len(recipe["people"]), "directed": True}, ensure_ascii=False))
        return
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".runtime") or output.exists():
        parser.error("Use a fresh output path inside .runtime")
    output.parent.mkdir(parents=True, exist_ok=True)
    origin = Settings()
    if origin.api_base.rstrip("/") != "https://api.deepseek.com" or not origin.generation_key():
        parser.error("Official DeepSeek credentials are unavailable")
    if origin.daily_budget_usd != 6:
        parser.error("Expected the authorized $6 project daily cap")
    base, name = os.environ.get(
        "TEST_DATABASE_URL", "postgresql+psycopg://continuum:continuum_local@127.0.0.1:55432/continuum_test"
    ).rsplit("/", 1)
    if (
        not name.endswith("_test")
        or not base.startswith("postgresql+psycopg://")
        or "@127.0.0.1:" not in base
    ):
        parser.error("Only a local explicitly named *_test database server is allowed")
    database_name = "sceneweaver_directed_" + uuid4().hex[:10] + "_test"
    admin = create_engine(
        base + "/postgres", isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5}
    )
    db = None
    budget_db = None
    budget_usage = None
    preserve_database = False
    created = False
    accounting_phase = 'not_started'
    budget_status = 'not_attempted'

    def record_pending(reason):
        original_error = sys.exception()
        try:
            output.with_name(output.stem + "-cleanup-pending.json").write_text(json.dumps({
                "database_name": database_name, "reason": reason,
                "accounting_phase": accounting_phase, "project_budget_status": budget_status,
                "project_budget_reservation": budget_usage,
            }, indent=2), "utf-8")
        except Exception:
            if original_error is None:
                raise
            # A secondary receipt error must not hide the accounting/cleanup failure.
            original_error.add_note('Cleanup-pending receipt could not be persisted; owned database retained.')

    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{database_name}"'))
            created = True
        config = origin.model_copy(
            update={
                "product": "roleplay",
                "database_url": base + "/" + database_name,
                "data_dir": output.parent / (output.stem + "-data"),
                "daily_budget_usd": 0.3,
                "turn_budget_usd": 0.15,
                "max_calls_per_task": 48,
                "embedding_provider": "lexical",
                "tts_enabled": False,
                "openrouter_roles": {},
            }
        )
        execution_identity(config, require_defaults=args.require_default_execution)
        db = Database(config)
        db.initialize()
        budget_db = Database(origin)
        budget_usage = reserve_acceptance_budget(budget_db, origin, 0.3)
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        report = asyncio.run(run(config, db, output))
        print(
            json.dumps(
                {
                    "passed": report.get("passed"),
                    "task": report.get("task"),
                    "cost_usd": report.get("cost_usd"),
                },
                ensure_ascii=False,
            )
        )
        if not report.get("passed"):
            raise SystemExit(1)
    finally:
        try:
            if db is not None and budget_usage is not None:
                preserve_database = True
                accounting_phase = 'ledger'
                final_ledger = ledger(db, 0.3)
                accounting_phase = 'ledger_receipt'
                output.with_name(output.stem + "-ledger.json").write_text(
                    json.dumps(final_ledger, ensure_ascii=False, indent=2), "utf-8"
                )
                conservative_cost = final_ledger["cost_usd"] + final_ledger["reserved_usd"]
                accounting_phase = 'settlement'
                budget_status = 'unknown'
                settle_acceptance_budget(budget_db, budget_usage, conservative_cost)
                budget_status = 'completed'
                accounting_phase = 'budget_receipt'
                output.with_name(output.stem + "-project-budget.json").write_text(json.dumps({
                    "usage_id": budget_usage, "reserved_cap_usd": 0.3,
                    "conservative_cost_usd": conservative_cost, "remaining_reservation_usd": 0,
                }, indent=2), "utf-8")
                accounting_phase = 'completed'
                preserve_database = False
        finally:
            if db:
                db.close()
            if budget_db:
                budget_db.close()
            try:
                if created and not preserve_database:
                    try:
                        with admin.connect() as conn:
                            conn.execute(text(f'DROP DATABASE "{database_name}"'))
                    except Exception:
                        record_pending('Normal database cleanup failed; preserve the owned database without forcing.')
                        raise
                elif created:
                    record_pending('Acceptance accounting did not finish; preserve evidence and verify budget status.')
            finally:
                admin.dispose()


if __name__ == "__main__":
    main()
