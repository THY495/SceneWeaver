"""Known reply opportunities within an already authorized, bounded plan."""


def reply_capacity(plan, index, visible_ids, segment_capacity):
    if segment_capacity is None:
        return None  # Historical ledgers did not record the remaining beat cap.
    return {
        "scheduled_reply_actor_ids": list(
            dict.fromkeys(job.actor for job in plan.jobs[index + 1 :] if job.actor in visible_ids)
        ),
        "can_schedule_more": not plan.handoff and segment_capacity > len(plan.jobs),
    }


def unavailable_npc_request(delivery, capacity, player_id):
    target = delivery.get("addressee_id")
    return bool(
        capacity is not None
        and delivery.get("expects_reply")
        and target
        and target != player_id
        and target not in capacity["scheduled_reply_actor_ids"]
        and not capacity["can_schedule_more"]
    )
