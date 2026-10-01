"""Bounded director performance: parallel preparation, ordered world commits."""

import asyncio

from sqlalchemy import select

from ..directing import DirectorPlan
from ..director_state import CandidateCommit, save_candidate
from ..observations import observed_record
from ..runtime import StaleTask, publication_gate_reason
from ..storage import Chunk, Conversation, Document, Operation, SceneRecord, Story, now
from .assignment import authorize_assignment
from .performance import UnsafeDraftExhausted, perform_actor
from .planning import plan_actor


def preparation_gate(services, task, turn, step, stage):
    """Stop autonomous preparation once the foreground no longer permits it."""
    if not getattr(task, "payload", {}).get("autonomous"):
        return None
    with services.db.session.begin() as s:
        story = s.get(Story, turn.story_id)
        if not story:
            raise StaleTask("演出剧情已失效。")
        player = s.get(Conversation, turn.player_id) if turn.player_id else None
        presence = (
            player.current_state.get("presence", {})
            if player
            else {"until": story.settings.get("activity_until", 0), "visible": True}
        )
        reason = publication_gate_reason(task, story, presence, now().timestamp())
        if reason:
            gate_id = f"{task.id}:preparation-gate:{step}:{stage}"
            if not s.get(Operation, gate_id):
                s.add(
                    Operation(
                        id=gate_id,
                        task_id=task.id,
                        payload={"reason": reason, "stage": stage},
                    )
                )
        return reason


def needs_player_before_next_job(intent, player_id):
    """An actor's ordinary segment ending must not preempt director jobs.

    A concrete request to the player or a required player action can do so.
    ``wait_for_player`` alone is too broad: actor planning sets it even for a
    normal answer addressed to the player, before other NPCs get their turn.
    """
    return bool(
        player_id
        and intent.get("expects_reply")
        and intent.get("addressee_id") == player_id
        and (intent.get("wait_for_player") or intent.get("awaiting_player_action"))
    )


async def execute_plan(services, task, turn, ledger, memory):
    plan = DirectorPlan.model_validate(ledger["plan"])
    offset, version = ledger.get("step_offset", 0), ledger["revision"]
    pending, replies, completed = {}, [], set()
    stopped = "player_turn" if plan.handoff else "limit"
    slots = asyncio.Semaphore(2)

    async def prepare(index, attempt):
        async with slots:
            job = plan.jobs[index]
            services.runtime.check(task.id, task.lease_version)
            step = offset + index
            if reason := preparation_gate(services, task, turn, step, "before_assignment"):
                return {"_paused": reason}
            with services.db.session() as s:
                actor = s.get(Conversation, job.actor)
                if not actor or actor.story_id != turn.story_id or actor.current_state.get("player"):
                    raise StaleTask("演出角色已失效。")
            scope = f"director:{version}:{job.id}:{attempt}:"
            assignment = await authorize_assignment(services, task, turn, actor, job, operation_scope=scope)
            if reason := preparation_gate(services, task, turn, step, "before_intent"):
                return {"_paused": reason}
            intent = await plan_actor(
                services,
                task,
                turn,
                actor,
                memory=memory,
                replies=list(replies),
                step=offset + index,
                limit=offset + len(plan.jobs),
                last_actor=None,
                continuation="",
                director_assignment=assignment,
                operation_scope=scope,
            )
            if intent is None:
                return None
            if reason := preparation_gate(services, task, turn, step, "before_actor"):
                return {"_paused": reason}
            if intent.get("act") == "object_action":
                from ..action_attempts import reserve

                reserve(services.db, task, actor.id, offset + index, intent.get("object_action") or {})
            voice_service = getattr(services, "voice", None)
            voice = ""
            if voice_service:
                from ..voice import actor_voice

                with services.db.session() as s:
                    voice = actor_voice(s, s.get(Conversation, actor.id), services.config)
            reply = await perform_actor(
                services,
                task,
                turn,
                actor,
                intent,
                offset + index,
                operation_scope=scope,
                voice_service=voice_service if voice else None,
                voice=voice,
                director_assignment=assignment,
            )
            # Actor planning precedes the final utterance. The semantic reviewer
            # may find that a planned player action was never actually requested.
            # Do not let that stale plan interrupt the remaining director jobs.
            if not (intent.get("expects_reply") and intent.get("addressee_id") == turn.player_id):
                intent["awaiting_player_action"] = False
            return {
                "text": reply["answer"],
                "revision": reply["revision"],
                "intent": intent,
                "evidence": reply.get("evidence", []),
                "claims": reply.get("claims", []),
                "spoken_parts": reply.get("spoken_parts", []),
                "performance": reply.get("performance", []),
                "_audio": reply.get("_candidate_audio"),
                "_audio_status": reply.get("_audio_status", "not_requested"),
            }

    try:
        for index, job in enumerate(plan.jobs):
            with services.db.session() as s:
                saved = s.get(Operation, f"{task.id}:committed:{offset + index}")
                if saved:
                    replies.append(saved.payload)
                    completed.add(job.id)
                    if needs_player_before_next_job(saved.payload["intent"], turn.player_id):
                        stopped = "player_turn"
                        break
                    continue
            for pos, ready in enumerate(plan.jobs):
                if pos >= index and ready.id not in pending and set(ready.depends_on) <= completed:
                    pending[ready.id] = asyncio.create_task(prepare(pos, 0))
            if job.id not in pending:
                raise StaleTask("前序演出依赖尚未完成。")
            candidate = await pending[job.id]
            if candidate and candidate.get("_paused"):
                stopped = "paused"
                break
            if candidate is None:
                stopped = "quiet"
                break
            with services.db.session() as s:
                actor = s.get(Conversation, job.actor)
                changed = actor.revision != candidate["revision"]
            if changed:
                candidate = await prepare(index, 1)
                if candidate and candidate.get("_paused"):
                    stopped = "paused"
                    break
                if candidate is None:
                    stopped = "quiet"
                    break
            with services.db.session() as s:
                current = s.get(Operation, f"{task.id}:director-ledger").payload
                seq = s.get(Story, turn.story_id).seq
                if ledger.get("recovery_reason") and s.scalar(
                    select(SceneRecord.id)
                    .where(
                        SceneRecord.story_id == turn.story_id,
                        SceneRecord.task_id == task.id,
                        SceneRecord.actor_id == job.actor,
                        SceneRecord.kind == "speech",
                        SceneRecord.text == candidate["text"],
                    )
                    .limit(1)
                ):
                    # Recovery must not replay an already published utterance.
                    # Keep the original event; never silently skip dependencies.
                    raise UnsafeDraftExhausted("恢复计划重复了本轮该角色已正式发表的台词，未再次发布。")
                for evidence in candidate["evidence"]:
                    chunk = s.get(Chunk, evidence["chunk_id"])
                    doc = s.get(Document, chunk.document_id) if chunk else None
                    if not doc or doc.deleted or doc.version != evidence["version"]:
                        raise StaleTask("演出引用资料已变化。")
            audio = candidate.pop("_audio", None)
            audio_status = candidate.pop("_audio_status", "not_requested")
            stored = save_candidate(
                services.db,
                task,
                turn.story_id,
                seq,
                plan_revision=version,
                job_id=job.id,
                expected_revision=current["candidates"].get(job.id, {}).get("revision", 0),
                dependency_revisions={dep: current["candidates"][dep]["revision"] for dep in job.depends_on},
                candidate=candidate,
            )
            event_id = services.runtime.commit_role_segment(
                task,
                job.actor,
                step=offset + index,
                director_candidate=CandidateCommit(version, job.id, stored["revision"]),
                prepared_audio=audio,
                prepared_audio_status=audio_status,
                **candidate,
            )
            if not event_id:
                stopped = "paused"
                break
            replies.append(
                {
                    "actor_id": job.actor,
                    "text": candidate["text"],
                    "event_id": event_id,
                    "intent": candidate["intent"],
                }
            )
            completed.add(job.id)
            if needs_player_before_next_job(candidate["intent"], turn.player_id):
                stopped = "player_turn"
                break
    finally:
        for future in pending.values():
            if not future.done():
                future.cancel()
        await asyncio.gather(*pending.values(), return_exceptions=True)
    with services.db.session() as s:
        from ..event_projection import requests_for

        unresolved = [
            r
            for r in (requests_for(s, turn.player_id) if turn.player_id else [])
            if (r["task_id"] == task.id or r["question_id"] == task.payload.get("resume_event_id"))
            and r["status"] in {"open", "partial"}
            and r["target_id"] != turn.player_id
        ]
        visible = []
        for reply in replies:
            view = observed_record(s, turn.player_id, reply["event_id"]) if turn.player_id else None
            if view:
                visible.append(
                    {
                        **reply,
                        "actor_id": view.actor_id,
                        "text": view.text,
                        "intent": {} if view.payload.get("perception") else reply["intent"],
                    }
                )
    return {
        "experiment": {
            "outcome": "committed" if replies else "wait",
            "event_ids": [r["event_id"] for r in replies],
        }
        if task.kind == "npc_step"
        else None,
        "incremental": True,
        "route": "player" if turn.player_id else "scene",
        "answer": "\n\n".join(r["text"] for r in visible),
        "segments": visible,
        "actor_ids": [r["actor_id"] for r in visible],
        "evidence": [],
        "stop_reason": stopped,
        "interaction_complete": bool(visible) and not unresolved,
        "pending_reply_ids": [r["question_id"] for r in unresolved],
        "director_plan_revision": version,
    }
