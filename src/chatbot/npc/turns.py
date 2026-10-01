"""Bounded public-scene turns. Actor state is never pooled into a director prompt."""

from sqlalchemy import select

from ..observability.timing import annotate, timed
from ..providers import ProviderError
from ..storage import Chunk, Conversation, Document, Message, Operation, RoleMemory, Story, Task, now
from .context import NPCServices, TurnContext
from .performance import UnsafeDraftExhausted, perform_actor
from .planning import plan_actor


def apply_continuation_boundary(intent):
    if intent.get("awaiting_player_action"):
        intent["wait_for_player"] = True
    elif intent.get("remaining_request"):
        intent["wait_for_player"] = False


@timed("npc.turn")
async def respond_player(services: NPCServices, task: Task):
    from ..role_memory import RoleMemoryService

    db, runtime = services.db, services.runtime
    memory = RoleMemoryService(db, services.config, runtime, services.provider)
    with db.session() as s:
        cached = s.get(Operation, f"{task.id}:final")
        if cached:
            return cached.payload
        scene_task = task.kind in {"scene_run", "npc_step"}
        player = None if scene_task else s.get(Conversation, task.session_id)
        story_id = task.payload["story_id"] if scene_task else player.story_id
        player_id = (
            task.payload.get("player_id") if task.kind == "npc_step" else None if scene_task else player.id
        )
        actors = list(
            s.scalars(
                select(Conversation)
                .where(
                    Conversation.story_id == story_id,
                    Conversation.owner_id == task.owner_id,
                    Conversation.id != player_id,
                )
                .order_by(Conversation.created_at, Conversation.id)
            )
        )
        actors = [a for a in actors if not a.current_state.get("player")]
        # Rotate by the preceding successfully committed turn, not submission attempts.
        previous = s.scalar(
            select(Message)
            .where(
                Message.session_id == player_id,
                Message.role == "assistant",
                Message.redacted.is_(False),
                Message.task_id.is_not(None),
            )
            .order_by(Message.seq.desc())
            .limit(1)
        )
        prior_task = s.get(Task, previous.task_id) if previous and previous.task_id else None
        prior_ids = (prior_task.result or {}).get("actor_ids", []) if prior_task else []
    if not actors:
        raise ProviderError("本剧情没有可发言的NPC。")
    if "audience" in task.payload:
        actors = [a for a in actors if a.id in task.payload["audience"]]
    if not actors:
        raise ProviderError("当前发言范围内没有可回应的NPC。")
    turn = TurnContext(story_id, player_id, tuple(a.id for a in actors))
    from .directed_scene import respond_directed
    from .director import snapshot

    with db.session() as s:
        directed = snapshot(s, task, turn)
        ledger = s.get(Operation, f"{task.id}:director-ledger")
        saved_plan = ledger.payload if ledger else None
    # A private, targeted item protocol is not a scene-wide director beat.
    # The recipient must decide the pending proposal before the director may
    # resume ordinary ensemble performance.
    if directed is not None and not (
        task.payload.get("handoff_proposal_id") or task.payload.get("item_result_event_id")
    ):
        voice_requested = bool(services.config.tts_enabled and task.payload.get(
            "voice_enabled", task.payload.get("acknowledgement", False)
        ))
        annotate(voice_requested=voice_requested)
        if voice_requested:
            from dataclasses import replace

            from ..voice import VoiceService

            voice = VoiceService(db, services.config, runtime)
            try:
                voice_available = voice.available()
                annotate(voice_available=voice_available)
                scoped = replace(services, voice=voice) if voice_available else services
                return await respond_directed(scoped, task, turn, memory, saved_plan)
            finally:
                await voice.close()
        return await respond_directed(services, task, turn, memory, saved_plan)
    mentioned = sorted(
        [a for a in actors if a.current_state["profile"]["name"] in task.payload["text"]],
        key=lambda a: task.payload["text"].index(a.current_state["profile"]["name"]),
    )
    addressed = [a for aid in task.payload.get("addressees", []) for a in actors if a.id == aid]
    if addressed:
        mentioned = [a for a in mentioned if a.id in {p.id for p in addressed}] or addressed
    runtime.publish(
        task.id,
        task.lease_version,
        "status",
        {
            "stage": "player",
            "message": "正在准备回应",
        },
    )

    replies, evidence, stopped = [], [], "limit"
    last_actor, requested, continuation = None, None, ""
    with db.session() as s:
        story = s.get(Story, turn.story_id)
        limit = max(1, min(3, story.settings.get("chain_limit", 3)))
        if task.kind == "npc_step":
            limit = 1
    for step in range(limit):
        runtime.check(task.id, task.lease_version)
        with db.session() as s:
            saved = s.get(Operation, f"{task.id}:committed:{step}")
            if saved:
                replies.append(saved.payload)
                last_actor = saved.payload["actor_id"]
                continuation = saved.payload["intent"].get("remaining_request", "")
                requested = (
                    saved.payload["intent"].get("addressee_id")
                    if saved.payload["intent"].get("expects_reply")
                    else last_actor
                    if saved.payload["intent"].get("remaining_request")
                    else saved.payload["intent"].get("addressee_id")
                )
                if saved.payload["intent"].get("wait_for_player"):
                    stopped = "player_turn"
                    break
                continue
            story = s.get(Story, turn.story_id)
            live = s.get(Conversation, turn.player_id) if turn.player_id else None
            presence = (
                live.current_state.get("presence", {})
                if live
                else {"until": story.settings.get("activity_until", 0), "visible": True}
            )
            foreground_reply = task.kind == "respond" and not task.payload.get("autonomous")
            if task.kind != "npc_step" and (
                story.settings.get("paused")
                or story.settings.get("narrative", {}).get("execution", {}).get("phase")
                in {"opening", "video"}
                or (
                    not foreground_reply
                    and presence.get("until", 0) > now().timestamp()
                    and (presence.get("typing") or not presence.get("visible"))
                )
            ):
                stopped = "paused"
                break
            if (
                task.kind != "npc_step"
                and task.payload.get("autonomous")
                and presence.get("until", 0) <= now().timestamp()
            ):
                stopped = "away"
                break
            goals = set(
                s.scalars(
                    select(RoleMemory.actor_id).where(
                        RoleMemory.story_id == turn.story_id,
                        RoleMemory.kind.in_(["goal", "commitment"]),
                        RoleMemory.status == "active",
                    )
                )
            )
        candidates = [a for a in (mentioned or actors) if a.id != last_actor]
        if requested in {a.id for a in actors}:
            candidates = [a for a in actors if a.id == requested] + [
                a for a in candidates if a.id != requested
            ]
        elif not mentioned:
            candidates.sort(key=lambda a: (a.id not in goals, a.id in prior_ids))
        if task.payload.get("autonomous") and step == 0:
            candidates = [
                a for a in candidates if a.id in goals or a.id == task.payload.get("trigger_actor_id")
            ]
        if task.kind == "npc_step":
            candidates = [a for a in actors if a.id == task.payload["step_actor_id"]]
        if not candidates:
            stopped = "quiet"
            break
        chosen, intent = None, None
        # Only relevant candidates invoke a model; no all-NPC heartbeat loop.
        for actor in candidates[:2]:
            intent = await plan_actor(
                services,
                task,
                turn,
                actor,
                memory=memory,
                replies=replies,
                step=step,
                limit=limit,
                last_actor=last_actor,
                continuation=continuation,
            )
            if intent is None:
                continue
            chosen = actor
            break
        if chosen is None:
            stopped = "quiet"
            break
        apply_continuation_boundary(intent)
        if intent.get("act") == "object_action":
            from ..action_attempts import reserve

            reserve(db, task, chosen.id, step, intent.get("object_action") or {})
        try:
            reply = await perform_actor(services, task, turn, chosen, intent, step)
        except UnsafeDraftExhausted:
            # An explicit experiment step may decline publication. A player task
            # may also finish incomplete once earlier approved segments are live;
            # never turn an entirely rejected response into a successful wait.
            if task.kind != "npc_step" and not replies:
                raise
            stopped = "safety_rejected"
            break
        apply_continuation_boundary(intent)
        with db.session() as s:
            for item in reply["evidence"]:
                chunk = s.get(Chunk, item["chunk_id"])
                doc = s.get(Document, chunk.document_id) if chunk else None
                if not doc or doc.deleted or doc.version != item["version"]:
                    raise ProviderError("NPC引用资料已变化，尚未提交该片段。")
        evidence = list({e["id"]: e for e in [*evidence, *reply["evidence"]]}.values())
        eid = runtime.commit_role_segment(
            task,
            chosen.id,
            reply["answer"],
            step=step,
            revision=reply["revision"],
            intent=intent,
            evidence=evidence,
            claims=reply.get("claims", []),
            spoken_parts=reply.get("spoken_parts", []),
            performance=reply.get("performance", []),
        )
        if not eid:
            from ..action_attempts import pause_task

            pause_task(db, task, "activity_paused")
            stopped = "paused"
            break
        replies.append({"actor_id": chosen.id, "text": reply["answer"], "event_id": eid, "intent": intent})
        continuation = intent.get("remaining_request", "")
        last_actor, requested = (
            chosen.id,
            (
                intent.get("addressee_id")
                if intent.get("expects_reply")
                else chosen.id
                if intent.get("remaining_request")
                else intent.get("addressee_id")
            ),
        )
        if intent["wait_for_player"]:
            stopped = "player_turn"
            break
    from ..event_projection import requests_for

    with db.session() as s:
        from ..observations import observed_record

        delivered_replies = []
        for reply in replies:
            view = observed_record(s, turn.player_id, reply["event_id"])
            if view:
                delivered_replies.append(
                    {
                        **reply,
                        "actor_id": view.actor_id,
                        "text": view.text,
                        "intent": {} if view.payload.get("perception") else reply["intent"],
                    }
                )
        unresolved = [
            r
            for r in (requests_for(s, turn.player_id) if turn.player_id else [])
            if (r["task_id"] == task.id or r["question_id"] == task.payload.get("resume_event_id"))
            and r["status"] in {"open", "partial"}
            and r["target_id"] != turn.player_id
        ]
    if replies and stopped not in {"paused", "safety_rejected"}:
        from ..auto_routing import assess

        await assess(db, services.provider, task)
    return {
        "experiment": {
            "outcome": "committed" if replies else "wait",
            "event_ids": [r["event_id"] for r in replies],
        }
        if task.kind == "npc_step"
        else None,
        "interaction_complete": bool(delivered_replies)
        and not unresolved
        and stopped != "safety_rejected",
        "pending_reply_ids": [r["question_id"] for r in unresolved],
        "incremental": True,
        "route": "scene" if scene_task else "player",
        "actor_ids": [r["actor_id"] for r in delivered_replies],
        "answer": "\n\n".join(r["text"] for r in delivered_replies),
        "evidence": evidence if len(delivered_replies) == len(replies) else [],
        "stop_reason": stopped,
        "segments": delivered_replies,
    }
