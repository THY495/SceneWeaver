"""Host-authored observations; no automatic cross-character knowledge propagation."""

from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .providers import estimate_tokens
from .schemas import PlayerStart, StoryEventInput
from .storage import Conversation, Story, StoryEvent, Task, uid


def story_router(db, config, runtime, owner):
    router = APIRouter(prefix="/api/stories")

    def host(who=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404, "剧情属于角色扮演子项目")
        return who

    def find(s, story_id, who):
        story = s.scalar(select(Story).where(Story.id == story_id, Story.owner_id == who).with_for_update())
        if not story:
            raise KeyError("剧情不存在")
        return story

    @router.get("")
    def listing(who=Depends(host)):
        with db.session() as s:
            return [
                {"id": r.id, "title": r.title}
                for r in s.scalars(
                    select(Story).where(Story.owner_id == who).order_by(Story.created_at.desc())
                )
            ]

    @router.get("/{story_id}")
    def detail(story_id: str, who=Depends(host)):
        with db.session() as s:
            story = find(s, story_id, who)
            participants = [
                {
                    "id": c.id,
                    "name": c.current_state.get("profile", {}).get("name", c.title),
                    "player": bool(c.current_state.get("player")),
                    "facts": c.current_state.get("scene_facts", {}),
                    "claims": c.current_state.get("scene_claims", {}),
                }
                for c in s.scalars(
                    select(Conversation)
                    .where(Conversation.story_id == story.id, Conversation.owner_id == who)
                    .order_by(Conversation.created_at)
                )
            ]
            events = [
                {"id": e.id, "recorded_at": e.created_at, **e.payload["request"]}
                for e in s.scalars(
                    select(StoryEvent)
                    .where(StoryEvent.story_id == story.id)
                    .order_by(StoryEvent.created_at.desc())
                    .limit(100)
                )
            ]
            return {
                "id": story.id,
                "title": story.title,
                "world": story.world,
                "participants": participants,
                "events": events,
            }

    @router.post("/{story_id}/player", status_code=201)
    async def start_player(story_id: str, body: PlayerStart, who=Depends(host)):
        name = body.name.strip()
        if not name:
            raise ValueError("请输入玩家角色名。")
        await runtime.model_readiness.require()
        with db.session.begin() as s:
            story = find(s, story_id, who)
            members = list(s.scalars(select(Conversation).where(Conversation.story_id == story.id)))
            prior = next((c for c in members if c.current_state.get("player")), None)
            if prior:
                if prior.current_state["profile"]["name"] != name:
                    raise HTTPException(409, "这段剧情已有玩家身份，请进入已有玩家会话。")
                return {"id": prior.id}
            if not members or len(members) > 8:
                raise ValueError("玩家剧情需要1至8名NPC。")
            if any(c.current_state.get("profile", {}).get("name") == name for c in members):
                raise ValueError("玩家名字不能与NPC相同。")
            c = Conversation(
                id=uid(),
                owner_id=who,
                story_id=story.id,
                title=name + " · 玩家剧情",
                epoch=0,
                seq=0,
                revision=1,
                current_state={
                    "player": True,
                    "profile": {
                        "name": name,
                        "core": "此角色由用户控制。",
                        "world": story.world,
                        "relationships": [],
                        "document_ids": [],
                    },
                },
            )
            s.add(c)
            runtime.emit(s, c, "player_started", {"name": name})
            return {"id": c.id}

    @router.post("/{story_id}/events", status_code=201)
    def deliver(story_id: str, body: StoryEventInput, who=Depends(host)):
        request = body.model_dump()
        ids = [r.session_id for r in body.recipients]
        if len(set(ids)) != len(ids):
            raise ValueError("同一事件不能重复列出同一接收者。")
        with db.session.begin() as s:
            find(s, story_id, who)
            prior = s.scalar(
                select(StoryEvent).where(
                    StoryEvent.story_id == story_id, StoryEvent.request_id == body.request_id
                )
            )
            if prior:
                if prior.payload["request"] != request:
                    raise HTTPException(409, "事件请求已提交，不能以同一标识改变内容。")
                return {"id": prior.id, "recipients": ids}
            sessions = list(
                s.scalars(
                    select(Conversation)
                    .where(
                        Conversation.story_id == story_id,
                        Conversation.owner_id == who,
                    )
                    .order_by(Conversation.id)
                    .with_for_update()
                )
            )
            targets = [c for c in sessions if c.id in ids]
            if len(targets) != len(ids) or any(not c.current_state.get("profile") for c in targets):
                raise ValueError("接收者必须是本剧情中已配置的角色，不能投递到另一段故事。")
            event_id = uid()
            recipients = {r.session_id: r for r in body.recipients}
            for c in targets:
                recipient = recipients[c.id]
                state = deepcopy(c.current_state)
                from .observations import current_knowledge, visible_records

                state["scene_facts"], state["scene_claims"] = current_knowledge(c, visible_records(s, c.id))
                observation = {
                    "key": body.key,
                    "value": body.value,
                    "event_id": event_id,
                    "occurred_at": body.occurred_at,
                    "learned_at": recipient.learned_at,
                    "basis": recipient.basis,
                    "source": recipient.source,
                }
                if recipient.basis in {"witnessed", "confirmed"}:
                    facts = state.setdefault("scene_facts", {})
                    current = facts.get(body.key)
                    if (current and recipient.replaces_event_id != current["event_id"]) or (
                        not current and recipient.replaces_event_id
                    ):
                        raise HTTPException(409, "角色对此事项的状态已变化，请刷新后明确替换当前事实。")
                    facts[body.key] = observation
                    # Confirming an issue retires earlier reports from the working set.
                    # Their original deliveries remain in the append-only event history.
                    claims = state.get("scene_claims", {})
                    observation["resolved_claim_ids"] = [
                        eid for eid, claim in claims.items() if claim["key"] == body.key
                    ]
                    state["scene_claims"] = {
                        eid: claim for eid, claim in claims.items() if claim["key"] != body.key
                    }
                else:
                    if recipient.replaces_event_id:
                        raise ValueError("转述或传闻不能替换已确认事实。")
                    state.setdefault("scene_claims", {})[event_id] = observation
                for field in ("scene_facts", "scene_claims"):
                    state[field] = dict(list(state.get(field, {}).items())[-30:])
                # Evicted entries remain in the observation archive and can be retrieved.
                while estimate_tokens(state) > config.context_hard_tokens // 2:
                    field = max(("scene_facts", "scene_claims"), key=lambda k: len(state[k]))
                    removable = [k for k, v in state[field].items() if v["event_id"] != event_id]
                    if not removable:
                        break
                    state[field].pop(removable[0])
                if estimate_tokens(state) > config.context_hard_tokens // 2:
                    raise ValueError("角色当前状态超过容量，本次事件尚未投递，请精简后重试。")
                c.current_state = state
                c.epoch += 1
                c.revision += 1
                for task in s.scalars(
                    select(Task).where(
                        Task.session_id == c.id,
                        Task.kind == "respond",
                        Task.status.in_(["queued", "running", "waiting"]),
                    )
                ):
                    task.status = "superseded"
                runtime.emit(s, c, "scene_updated", {"observation": observation})
                from .observations import append_record

                append_record(
                    s,
                    story_id=story_id,
                    kind="host",
                    text=body.value,
                    observer_ids=[c.id],
                    dedupe=f"host:{event_id}:{c.id}",
                    channel="private",
                    addressees=[c.id],
                    occurred_at=body.occurred_at,
                    payload=observation,
                )
            # A private NPC update also fences the shared turn without copying its contents.
            for c in sessions:
                if c.current_state.get("player") and c.id not in ids:
                    c.epoch += 1
                    c.revision += 1
                    for task in s.scalars(
                        select(Task).where(
                            Task.session_id == c.id,
                            Task.kind == "respond",
                            Task.status.in_(["queued", "running", "waiting"]),
                        )
                    ):
                        task.status = "superseded"
                    runtime.emit(s, c, "scene_updated", {"message": "剧情状态已更新，请继续。"})
            s.add(
                StoryEvent(
                    id=event_id, story_id=story_id, request_id=body.request_id, payload={"request": request}
                )
            )
        return {"id": event_id, "recipients": ids}

    return router
