"""Authenticated player actions against server state; caller-supplied worlds are never trusted."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select

from ..observations import append_record
from ..storage import Conversation, ItemWorldState, Story, StoryEvent
from .execution import commit, prepare, view
from .models import ItemCommand
from .persistence import load
from .rules import RuleViolation
from .transfers import Offer, create_offer, require_interactive, transfer_view


class Action(ItemCommand):
    request_id: str = Field(min_length=1, max_length=40)


def runtime_router(db, config, owner):
    router = APIRouter(prefix="/api/role/items")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    def context(s, aid, identity):
        actor = s.get(Conversation, aid)
        if not actor or actor.owner_id != identity or not actor.story_id:
            raise HTTPException(404, "角色不存在")
        story = s.scalar(
            select(Story).where(Story.id == actor.story_id, Story.owner_id == identity).with_for_update()
        )
        if not story:
            raise HTTPException(404, "故事不存在")
        return actor, story

    @router.get("/actors/{aid}")
    def inventory(aid: str, identity=Depends(who)):
        with db.session.begin() as s:
            actor, story = context(s, aid, identity)
            if s.get(ItemWorldState, story.id) is None:
                return {"enabled": False, "items": []}
            _, world = load(s, story.id)
            return {"enabled": True, "items": view(s, actor, world), **transfer_view(s, actor, world)}

    @router.post("/actors/{aid}/offers")
    def offer(aid: str, body: Offer, identity=Depends(who)):
        with db.session.begin() as s:
            actor, story = context(s, aid, identity)
            if not actor.current_state.get("player"):
                raise HTTPException(403, "只能替玩家本人提出递交。")
            try:
                from ..runtime import Runtime

                result = create_offer(s, actor, story, body, Runtime(db, config))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        return result

    @router.post("/actors/{aid}/actions")
    def action(aid: str, body: Action, identity=Depends(who)):
        with db.session.begin() as s:
            actor, story = context(s, aid, identity)
            if not actor.current_state.get("player"):
                raise HTTPException(403, "此入口仅控制玩家自己的行动。")
            key = "item:" + body.request_id
            old = s.scalar(
                select(StoryEvent).where(StoryEvent.story_id == story.id, StoryEvent.request_id == key)
            )
            request = {"actor_id": aid, **body.model_dump()}
            if old:
                if old.payload.get("request") != request:
                    raise HTTPException(409, "同一请求不能执行不同操作。")
                return old.payload["result"]
            try:
                if body.action == "give":
                    try:
                        require_interactive(s, actor, story)
                    except ValueError as exc:
                        raise HTTPException(409, str(exc)) from exc
                row, candidate, action_text = prepare(s, actor, body.model_dump(exclude={"request_id"}))
            except RuleViolation as exc:
                raise HTTPException(
                    400 if exc.code == "unknown_item" else 409, {"code": exc.code, "reason": str(exc)}
                ) from exc
            commit(s, actor, row, candidate, key)
            event = append_record(
                s,
                story_id=story.id,
                kind="action",
                actor=actor,
                text=action_text,
                observer_ids=[aid, body.destination.target] if body.action == "give" else [aid],
                channel="private",
                dedupe=f"item-action:{body.request_id}",
                payload={
                    "item_action_result": {
                        "status": "completed",
                        "command": body.model_dump(exclude={"request_id"}),
                    },
                    **(
                        {"agreement": {"type": "completion", **body.agreement.model_dump()}}
                        if body.agreement
                        else {}
                    ),
                    "item_action": {
                        "item_id": body.item_id,
                        "action": body.action,
                        "from_revision": body.expected_revision,
                        "to_revision": body.expected_revision + 1,
                    },
                },
            )
            result = {
                "ok": True,
                "item_id": body.item_id,
                "revision": body.expected_revision + 1,
                "items": view(s, actor, candidate),
            }
            if body.action == "give":
                from ..runtime import Runtime

                runtime = Runtime(db, config)
                actor.current_state = {
                    **actor.current_state,
                    "presence": {**actor.current_state.get("presence", {}), "typing": False},
                }
                task = runtime.enqueue(
                    s,
                    "respond",
                    identity,
                    {
                        "text": action_text + "请接收方回应这次实际交接。",
                        "mode": "auto",
                        "event_id": event.id,
                        "item_result_event_id": event.id,
                        "audience": [aid, body.destination.target],
                        "channel": "private",
                        "addressees": [body.destination.target],
                    },
                    session_id=aid,
                    epoch=actor.epoch,
                    priority=0,
                )
                runtime.emit(s, actor, "accepted", {"message": "交接已完成，等待回应"}, task.id)
                result["task_id"] = task.id
            s.add(
                StoryEvent(story_id=story.id, request_id=key, payload={"request": request, "result": result})
            )
            return result

    return router
