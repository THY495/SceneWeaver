"""Bounded director performance: parallel preparation, ordered world commits."""

import asyncio

from sqlalchemy import select

from ..directing import DirectorPlan
from ..director_recovery import bind_notice
from ..director_state import CandidateCommit, save_candidate
from ..observability.timing import annotate, measure
from ..observations import observed_record, perceived_cast
from ..runtime import StaleTask, publication_gate_reason
from ..storage import Chunk, Conversation, Document, Operation, SceneRecord, Story, now
from .assignment import AssignmentRejected, authorize_assignment
from .dialogue_capacity import reply_capacity
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


def needs_player_before_next_job(intent, player_id, handoff_policy="on_question"):
    """An actor's ordinary segment ending must not preempt director jobs.

    A required player action always can. Ordinary questions may wait until the
    authored beat boundary; legacy ledgers retain immediate question handoff.
    ``wait_for_player`` alone is too broad: actor planning sets it even for a
    normal answer addressed to the player, before other NPCs get their turn.
    """
    return bool(
        player_id
        and intent.get("expects_reply")
        and intent.get("addressee_id") == player_id
        and (
            intent.get("awaiting_player_action")
            or (intent.get("wait_for_player") and handoff_policy != "after_beat")
        )
    )


def open_task_questions(db, task_id, actor_id):
    """Questions newly addressed to this actor can invalidate parallel work."""
    from ..event_projection import requests_for

    with db.session() as s:
        return {
            request["question_id"]
            for request in requests_for(s, actor_id)
            if request["task_id"] == task_id
            and request["target_id"] == actor_id
            and request["status"] in {"open", "partial"}
        }


async def execute_plan(services, task, turn, ledger, memory):
    plan = DirectorPlan.model_validate(ledger["plan"])
    offset, version = ledger.get("step_offset", 0), ledger["revision"]
    handoff_policy = ledger.get("handoff_policy", "on_question")
    if handoff_policy not in {"after_beat", "on_question", "continuous"}:
        raise StaleTask("导演交还节奏无效，未继续演出。")
    pending, replies, completed = {}, [], set()
    stopped = "player_turn" if plan.handoff else "limit"
    preparation_concurrency = getattr(getattr(services, "config", None), "role_preparation_concurrency", 2)
    slots = asyncio.Semaphore(preparation_concurrency)

    async def prepare_work(index, attempt, preparation):
        async with slots:
            preparation.mark("slot_acquired")
            job = plan.jobs[index]
            services.runtime.check(task.id, task.lease_version)
            step = offset + index
            seen_questions = open_task_questions(services.db, task.id, job.actor)
            if reason := preparation_gate(services, task, turn, step, "before_assignment"):
                return {"_paused": reason}
            with services.db.session() as s:
                actor = s.get(Conversation, job.actor)
                if not actor or actor.story_id != turn.story_id or actor.current_state.get("player"):
                    raise StaleTask("演出角色已失效。")
                actor_revision = actor.revision
                privacy_version = actor.privacy_version
                world_seq = s.get(Story, turn.story_id).seq
            scope = f"director:{version}:{job.id}:{attempt}:"
            with services.db.session() as s:
                visible_ids = {person["id"] for person in perceived_cast(s, s.get(Conversation, actor.id))}
            capacity = reply_capacity(
                plan,
                index,
                visible_ids,
                None if task.kind == "npc_step" else ledger.get("segment_capacity"),
            )
            try:
                assignment = await authorize_assignment(
                    services, task, turn, actor, job, operation_scope=scope, dialogue_capacity=capacity
                )
            except AssignmentRejected as error:
                # A local repair may use actor-private material. Filter against
                # the original director job again before exporting any excerpt.
                error.recovery_notice = bind_notice(
                    task, ledger, job, actor_revision, privacy_version, world_seq,
                    [p.model_dump() for p in error.rejected_premises],
                )
                raise
            if capacity is not None:
                assignment = {**assignment, "dialogue_capacity": capacity}
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
                combine_speech=getattr(getattr(services, "config", None), "role_actor_generation", "separate")
                == "combined_context",
            )
            if intent is None:
                return None
            with services.db.session() as s:
                frame = s.get(Operation, f"{task.id}:direct-response-frame")
                direct_only = bool(frame and frame.payload.get("plan_revision") == version)
            if direct_only and intent.get("act") != "speak":
                # The author permission covers a reply, not a new action decision.
                # Recover through the existing director before reserving/executing an action.
                raise UnsafeDraftExhausted("直接回应提出了非发言行动，须交由导演重新编排。")
            initial_speech = intent.pop("_speech_draft", None)
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
                **({"initial_speech": initial_speech} if initial_speech is not None else {}),
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
                "_seen_open_questions": sorted(seen_questions),
            }

    async def prepare(index, attempt):
        with measure("npc.directed_preparation") as preparation:
            annotate(job_id=plan.jobs[index].id, actor_id=plan.jobs[index].actor,
                     candidate_revision=attempt, preparation_concurrency=preparation_concurrency)
            return await prepare_work(index, attempt, preparation)

    try:
        for index, job in enumerate(plan.jobs):
            with services.db.session() as s:
                saved = s.get(Operation, f"{task.id}:committed:{offset + index}")
                if saved:
                    replies.append(saved.payload)
                    completed.add(job.id)
                    if needs_player_before_next_job(saved.payload["intent"], turn.player_id, handoff_policy):
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
            new_questions = open_task_questions(services.db, task.id, job.actor) - set(
                candidate.get("_seen_open_questions", [])
            )
            if changed or any(question != candidate["intent"].get("reply_to") for question in new_questions):
                # A prior job may ask this actor something after parallel
                # preparation began. A candidate built before that question
                # cannot count as its answer merely because its words fit.
                if candidate.get("_audio") and getattr(services, "voice", None):
                    await services.voice.discard_candidate(candidate["_audio"])
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
            candidate.pop("_seen_open_questions", None)
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
            if needs_player_before_next_job(candidate["intent"], turn.player_id, handoff_policy):
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
        # This is an operational completion marker, not proof of semantic task
        # satisfaction. A successfully stopped worker may leave director jobs.
        "director_plan_complete": len(completed) == len(plan.jobs),
        "tracked_replies_resolved": not unresolved,
        "interaction_complete": bool(visible) and not unresolved and len(completed) == len(plan.jobs),
        "pending_reply_ids": [r["question_id"] for r in unresolved],
        "director_plan_revision": version,
    }
