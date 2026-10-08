"""Continue authored beats without stealing a player's requested turn."""

from sqlalchemy import select

from ..director_planning import RecoveryPlanIncomplete
from ..observations import observed_record
from ..runtime import StaleTask
from ..storage import Operation
from .assignment import AssignmentRejected, ReplyCapacityRejected
from .beat_progress import assess_beat
from .directed_turn import execute_plan, preparation_gate
from .director import prepare_scene_plan, snapshot
from .performance import ResponseContractConflict, UnsafeDraftExhausted


def remaining_in_beat(db, task_id, beat):
    from .exchange_requirement import beat_committed_event_ids

    with db.session() as s:
        events = beat_committed_event_ids(s, task_id, beat.id)
    return max(0, beat.max_segments - len(events))


async def execute_recoverable(services, task, turn, ledger, memory, before):
    try:
        return ledger, await execute_plan(services, task, turn, ledger, memory)
    except (AssignmentRejected, UnsafeDraftExhausted, StaleTask) as failure:
        # Interruption/expired lease must escape before any new model request.
        services.runtime.check(task.id, task.lease_version)
        if ledger.get("recovery_count", 0) >= 1:
            raise
        with services.db.session() as s:
            current = snapshot(s, task, turn)
            prefix = f"{task.id}:committed:"
            count = len(list(s.scalars(select(Operation.id).where(Operation.id.startswith(prefix)))))
        if (
            not current
            or current.node_id != before.node_id
            or current.entry_event_id != before.entry_event_id
            or count >= 6
        ):
            raise
        reason = (
            "assignment_rejected"
            if isinstance(failure, AssignmentRejected)
            else "draft_rejected"
            if isinstance(failure, UnsafeDraftExhausted)
            else "state_changed"
        )
        beat = next(b for b in current.direction.beats if b.id == current.beat_id)
        remaining = min(6 - count, remaining_in_beat(services.db, task.id, beat))
        if not remaining:
            raise
        replacement = await prepare_scene_plan(
            services,
            task,
            turn,
            max_jobs=remaining,
            recovery_reason=reason,
            recovery_hint="required_npc_reply_without_slot"
            if isinstance(failure, ReplyCapacityRejected)
            else "answer_only_with_intended_player_question"
            if isinstance(failure, ResponseContractConflict)
            else "",
            recovery_notice=getattr(failure, "recovery_notice", None),
        )
        return replacement, await execute_plan(services, task, turn, replacement, memory)


async def respond_directed(services, task, turn, memory, saved_plan=None):
    if not saved_plan and preparation_gate(services, task, turn, 0, "before_director"):
        return {
            "incremental": True,
            "route": "player" if turn.player_id else "scene",
            "answer": "",
            "segments": [],
            "actor_ids": [],
            "evidence": [],
            "stop_reason": "paused",
            "interaction_complete": False,
            "pending_reply_ids": [],
        }
    ledger = saved_plan or await prepare_scene_plan(services, task, turn)
    independent_route = None
    # Cap a response across beat boundaries, not just each model's short plan.
    for _ in range(6):
        with services.db.session() as s:
            before = snapshot(s, task, turn, beat_id=ledger["plan"]["beat_id"])
        try:
            ledger, result = await execute_recoverable(services, task, turn, ledger, memory, before)
        except (AssignmentRejected, UnsafeDraftExhausted, RecoveryPlanIncomplete):
            # The performance and the player's action are separate outcomes.
            # A rejected candidate never supplies route evidence or beat progress.
            with services.db.session() as s:
                committed = s.scalar(
                    select(Operation.id).where(Operation.id.startswith(f"{task.id}:committed:")).limit(1)
                )
            if not committed:
                if task.kind != "respond" or not turn.player_id:
                    raise
                from ..auto_routing import assess

                services.runtime.check(task.id, task.lease_version)
                independent_route = await assess(
                    services.db, services.provider, task, player_action_only=True
                )
                if not independent_route or not independent_route["accepted"]:
                    raise
            result = {
                "incremental": True,
                "route": "player" if turn.player_id else "scene",
                "evidence": [],
                "stop_reason": "safety_rejected",
                "interaction_complete": False,
                "pending_reply_ids": [],
            }
            break
        if result["stop_reason"] == "paused":
            break
        beat = next(b for b in before.direction.beats if b.id == before.beat_id)
        with services.db.session() as s:
            count = len(
                list(s.scalars(select(Operation.id).where(Operation.id.startswith(f"{task.id}:committed:"))))
            )
        remaining = min(6 - count, remaining_in_beat(services.db, task.id, beat))
        if (
            task.kind != "npc_step"
            and result["stop_reason"] == "limit"
            and ledger["plan"].get("jobs")
            and not ledger["plan"].get("handoff", True)
            and remaining > 0
        ):
            # A short plan ending is not a beat ending. Assess only after the
            # bounded suffix, so an early not_met result cannot stick in cache.
            ledger = await prepare_scene_plan(services, task, turn, max_jobs=remaining)
            continue
        assessment = await assess_beat(services, task, turn, ledger["plan"]["beat_id"])
        result["dialogue_requirement_met"] = assessment.get("npc_exchange_met")
        if result["dialogue_requirement_met"] is False:
            result["interaction_complete"] = False
        with services.db.session() as s:
            after = snapshot(s, task, turn)
            prefix = f"{task.id}:committed:"
            count = len(list(s.scalars(select(Operation.id).where(Operation.id.startswith(prefix)))))
        if (
            task.kind == "npc_step"
            or result["stop_reason"] in {"player_turn", "quiet"}
            or beat.handoff == "after_beat"
            or not assessment["advanced"]
            or not after
            or after.finished
            or count >= 6
        ):
            break
        ledger = await prepare_scene_plan(services, task, turn, max_jobs=6 - count)
    # Recover every committed segment, including earlier plans after a restart.
    with services.db.session() as s:
        prefix = f"{task.id}:committed:"
        rows = list(s.scalars(select(Operation).where(Operation.id.startswith(prefix))))
        rows.sort(key=lambda row: int(row.id[len(prefix) :]))
        visible = []
        for row in rows:
            reply = row.payload
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
        if result["stop_reason"] == "safety_rejected":
            from ..event_projection import requests_for

            result["pending_reply_ids"] = [
                request["question_id"]
                for request in (requests_for(s, turn.player_id) if turn.player_id else [])
                if request["task_id"] == task.id
                and request["status"] in {"open", "partial"}
                and request["target_id"] != turn.player_id
            ]
        from ..event_projection import requests_for

        # after_beat may defer a question across a short-plan suffix. Once the
        # bounded scene execution ends, keep it open and actually yield; never
        # let autonomous work treat the unanswered question as a continuation.
        waiting_for_player = [
            request["question_id"]
            for request in (requests_for(s, turn.player_id) if turn.player_id else [])
            if request["task_id"] == task.id
            and request["target_id"] == turn.player_id
            and request["status"] in {"open", "partial"}
        ]
        if waiting_for_player and result["stop_reason"] == "limit":
            result["stop_reason"] = "player_turn"
        result["pending_player_question_ids"] = waiting_for_player
    result.update(
        segments=visible,
        actor_ids=[r["actor_id"] for r in visible],
        answer="\n\n".join(r["text"] for r in visible),
    )
    if result["stop_reason"] == "safety_rejected" and turn.player_id and task.kind == "respond":
        if independent_route is None:
            from ..auto_routing import assess

            services.runtime.check(task.id, task.lease_version)
            independent_route = await assess(services.db, services.provider, task, player_action_only=True)
        result["player_action_route_accepted"] = bool(independent_route and independent_route["accepted"])
    if turn.player_id and task.kind != "npc_step" and result["stop_reason"] == "player_turn" and after:
        # A handoff remains in force until this world/entry/player turn changes.
        # Capture the assessed snapshot, not a later state after routing or I/O.
        result["director_handoff"] = {
            "world_seq": after.world_seq,
            "node_id": after.node_id,
            "entry_event_id": after.entry_event_id,
            "epoch": task.epoch,
        }
    if result["stop_reason"] not in {"paused", "safety_rejected"} and task.kind != "npc_step":
        from .beat_reuse import remember_frame

        remember_frame(services, task, turn, result)
        from ..auto_routing import assess

        await assess(services.db, services.provider, task)
    return result
