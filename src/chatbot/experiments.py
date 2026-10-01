"""One decision under a renewable, revocable permit; the story stays paused."""

from datetime import UTC

from fastapi import HTTPException
from sqlalchemy import func, select

from .observations import members
from .storage import Conversation, GameSave, Operation, Task, Usage, now

ACTIVE = ("queued", "running", "waiting")


def valid_step(s, task, story):
    if not story or task.kind != "npc_step" or task.owner_id != story.owner_id:
        return False
    # A committed receipt may be completed after a crash, but never authorizes another step.
    if s.get(Operation, f"{task.id}:committed:0"):
        return True
    permit = task.payload.get("permit", {})
    game_backstage = task.payload.get("game_backstage", False)
    if game_backstage:
        state = story.settings.get("narrative", {})
        progress = story.settings.get("game_backstage_progress", {})
        plan = story.settings.get("game_backstage_plan", {})
        if (
            state.get("mode") != "backstage"
            or state.get("node") != task.payload.get("backstage_node")
            or state.get("event_id") != task.payload.get("backstage_entry")
            or state.get("node") not in plan.get("directed", [])
            or progress.get("last_task") != task.id
            or not s.scalar(
                select(GameSave.story_id).where(
                    GameSave.story_id == story.id, GameSave.owner_id == story.owner_id
                )
            )
        ):
            return False
    if (
        not story.settings.get("paused")
        or (not game_backstage and permit.get("until", 0) <= now().timestamp())
        or task.deadline <= now()
    ):
        return False
    if story.seq != permit.get("story_seq"):
        return False
    people = members(s, story.id)
    versions = {c.id: [c.revision, c.epoch] for c in people}
    if game_backstage:
        actor = next((c for c in people if c.id == task.payload.get("step_actor_id")), None)
        if not actor:
            return False
        scene = actor.current_state.get("location", "main")
        present = {c.id for c in people if c.current_state.get("location", "main") == scene}
        if (
            scene != task.payload.get("scene")
            or present != set(task.payload.get("audience", []))
            or present != set(story.settings["narrative"]["participants"])
        ):
            return False
    return versions == permit.get("versions") and not any(
        c.current_state.get("presence", {}).get("typing")
        and c.current_state.get("presence", {}).get("until", 0) > now().timestamp()
        for c in people
        if c.current_state.get("player")
    )


def create_step(s, story, actor_id, request_id, runtime):
    from .scene_execution import opening_active, story_finished

    if story_finished(story):
        raise HTTPException(409, "故事已结束，请新建测试运行。")

    if opening_active(story):
        raise HTTPException(409, "开场尚未完成，不能插入 NPC 实验单步。")
    dedupe = f"npc-step:{story.id}:{request_id}"
    previous = s.scalar(select(Task).where(Task.dedupe_key == dedupe))
    if previous:
        if previous.payload["step_actor_id"] != actor_id:
            raise HTTPException(409, "相同请求标识不能改选角色。")
        return previous
    if not story.settings.get("paused"):
        raise HTTPException(409, "请先暂停剧情，再执行实验单步。")
    people = members(s, story.id)
    actor = next((c for c in people if c.id == actor_id and not c.current_state.get("player")), None)
    if not actor:
        raise HTTPException(404, "NPC不属于当前剧情。")
    narrative = story.settings.get("narrative", {})
    if narrative.get("mode") == "backstage":
        present = {
            c.id
            for c in people
            if c.current_state.get("location", "main") == actor.current_state.get("location", "main")
        }
        if actor.id not in narrative.get("participants", []) or present != set(narrative["participants"]):
            raise HTTPException(409, "幕后单步仅允许本场参与者，且参与者须保持同场、玩家不在场。")
    active = s.scalar(
        select(Task.id)
        .outerjoin(Conversation, Conversation.id == Task.session_id)
        .where(
            Task.status.in_(ACTIVE),
            Task.kind.in_(["respond", "scene_run", "npc_step"]),
            (Conversation.story_id == story.id) | (Task.payload["story_id"].as_string() == story.id),
        )
        .limit(1)
    )
    if active:
        raise HTTPException(409, "剧情仍有未结束任务，请先取消或等待完成。")
    day = now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    spent = s.scalar(
        select(func.coalesce(func.sum(Usage.cost + Usage.reserved), 0)).where(Usage.created_at >= day)
    )
    if spent >= runtime.config.daily_budget_usd:
        raise HTTPException(409, "今日API预算已用尽。每次模型请求仍会执行独立费用预留。")
    scene = actor.current_state.get("location", "main")
    cast = [c for c in people if c.current_state.get("location", "main") == scene]
    player = next((c for c in cast if c.current_state.get("player")), None)
    from .event_projection import requests_for

    pending = [
        request
        for request in requests_for(s, actor.id)
        if request["target_id"] == actor.id and request["status"] in {"open", "partial", "defer"}
    ]
    task = runtime.enqueue(
        s,
        "npc_step",
        story.owner_id,
        {
            "story_id": story.id,
            "scene": scene,
            "audience": [c.id for c in cast],
            "step_actor_id": actor.id,
            "player_id": player.id if player else None,
            "autonomous": True,
            "text": "基于自己已有的目标与观察，评估是否行动。",
            **({"resume_event_id": pending[-1]["question_id"]} if pending else {}),
            "permit": {
                "until": now().timestamp() + 15,
                "story_seq": story.seq,
                "versions": {c.id: [c.revision, c.epoch] for c in people},
            },
        },
        priority=45,
        dedupe=dedupe,
    )
    # Creation cannot race a closed client: only an explicit visible-client renewal arms it.
    task.status = "waiting"
    return task
