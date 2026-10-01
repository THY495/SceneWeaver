"""Roleplay-owned authoring; dialogue only consumes frozen session copies."""

from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from .providers import estimate_tokens
from .schemas import CharacterImport, CharacterStart, CharacterUpdate, SessionProfile
from .storage import CharacterCard, CharacterRevision, Conversation, Document, Event, Story, uid


def validate_profile(s, owner, profile, config):
    allowed = set(
        s.scalars(
            select(Document.id).where(
                Document.owner_id == owner,
                Document.deleted.is_(False),
                Document.status == "ready",
                Document.id.in_(profile.document_ids),
            )
        )
    )
    if not set(profile.document_ids) <= allowed:
        raise ValueError("角色资料不存在或尚未导入完成，请重新选择可知资料。")
    if estimate_tokens(profile.model_dump()) > config.context_hard_tokens // 3:
        raise ValueError("常驻设定过长，请精简世界概况、核心经历和关系，将详细内容放入角色资料。")


def character_router(db, config, runtime, owner):
    router = APIRouter(prefix="/api/characters")

    def author(who=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404, "角色卡属于角色扮演子项目")
        return who

    def find(s, cid, who):
        card = s.scalar(
            select(CharacterCard)
            .where(CharacterCard.id == cid, CharacterCard.owner_id == who)
            .with_for_update()
        )
        if not card:
            raise KeyError("角色卡不存在")
        return card

    def render(card):
        return {"id": card.id, "version": card.version, **card.data}

    @router.get("")
    def listing(who=Depends(author)):
        with db.session() as s:
            return [
                render(c)
                for c in s.scalars(
                    select(CharacterCard)
                    .where(CharacterCard.owner_id == who)
                    .order_by(CharacterCard.created_at.desc())
                )
            ]

    @router.post("", status_code=201)
    def create(body: SessionProfile, who=Depends(author)):
        with db.session.begin() as s:
            validate_profile(s, who, body, config)
            card = CharacterCard(id=uid(), owner_id=who, version=1, data=body.model_dump())
            s.add(card)
            s.flush()
            s.add(CharacterRevision(card_id=card.id, version=1, data=deepcopy(card.data)))
        return render(card)

    @router.post("/preview")
    def preview(body: CharacterImport, who=Depends(author)):
        with db.session() as s:
            validate_profile(s, who, body.profile, config)
        return body.profile.model_dump()

    @router.put("/{cid}")
    def update(cid: str, body: CharacterUpdate, who=Depends(author)):
        with db.session.begin() as s:
            card = find(s, cid, who)
            if card.version != body.expected_version:
                raise HTTPException(409, "角色卡已被修改，请重新加载后编辑。")
            profile = SessionProfile.model_validate(body.model_dump())
            validate_profile(s, who, profile, config)
            card.version += 1
            card.data = profile.model_dump()
            s.add(CharacterRevision(card_id=cid, version=card.version, data=deepcopy(card.data)))
        return render(card)

    @router.post("/{cid}/sessions", status_code=201)
    def start(cid: str, body: CharacterStart, who=Depends(author)):
        with db.session.begin() as s:
            find(s, cid, who)  # Serializes duplicate starts for this card.
            prior = s.scalar(
                select(Event)
                .join(Conversation, Conversation.id == Event.session_id)
                .where(
                    Conversation.owner_id == who,
                    Event.kind == "character_started",
                    Event.payload["card_id"].astext == cid,
                    Event.payload["request_id"].astext == body.request_id,
                )
                .limit(1)
            )
            if prior:
                if (
                    prior.payload["version"] != body.version
                    or prior.payload.get("requested_story_id") != body.story_id
                ):
                    raise HTTPException(409, "同一请求标识不能启动不同版本。")
                return {
                    "id": prior.session_id,
                    "card_id": cid,
                    "version": body.version,
                    "story_id": s.get(Conversation, prior.session_id).story_id,
                }
            revision = s.get(CharacterRevision, (cid, body.version))
            if not revision:
                raise KeyError("角色卡版本不存在")
            profile = SessionProfile.model_validate(revision.data)
            validate_profile(s, who, profile, config)
            if body.story_id:
                story = s.scalar(
                    select(Story).where(Story.id == body.story_id, Story.owner_id == who).with_for_update()
                )
                if not story:
                    raise KeyError("剧情不存在")
                if s.scalar(
                    select(Conversation.id)
                    .where(
                        Conversation.story_id == story.id,
                        Conversation.current_state["player"].astext == "true",
                    )
                    .limit(1)
                ):
                    raise ValueError("玩家剧情已开始，首版请在开始前配置完整的在场角色。")
                if story.world != profile.world:
                    raise ValueError("该角色卡的世界概况与剧情不同，请使用同一世界设定或开启新剧情。")
                existing_actor = s.scalar(
                    select(Conversation.id)
                    .where(
                        Conversation.story_id == story.id,
                        Conversation.current_state["character_origin"]["card_id"].astext == cid,
                    )
                    .limit(1)
                )
                if existing_actor:
                    raise HTTPException(409, "该角色已经在此剧情中，请切换到已有角色会话。")
            else:
                story = Story(id=uid(), owner_id=who, title=profile.name + " · 剧情", world=profile.world)
                s.add(story)
                s.flush()
            c = Conversation(
                id=uid(),
                owner_id=who,
                story_id=story.id,
                title=profile.name + " · 新剧情",
                epoch=0,
                seq=0,
                revision=1,
                current_state={
                    "profile": profile.model_dump(),
                    "scene_facts": {},
                    "character_origin": {"card_id": cid, "version": body.version},
                },
            )
            s.add(c)
            # Only an explicitly labelled current-goal line is seeded; no model invents startup motives.
            import re

            from .observations import append_record
            from .storage import RoleMemory

            initial = re.search(r"(?:^|\n)当前目标[：:]([^\n。]+)", profile.core)
            if initial:
                for index, goal in enumerate(re.split(r"[；;]", initial.group(1))[:3]):
                    goal = goal.strip()[:800]
                    if not goal:
                        continue
                    event = append_record(
                        s,
                        story_id=story.id,
                        kind="authoring",
                        text=goal,
                        observer_ids=[c.id],
                        channel="private",
                        dedupe=f"initial-goal:{c.id}:{index}",
                        payload={"kind": "goal", "card_id": cid, "card_version": body.version},
                    )
                    s.add(
                        RoleMemory(
                            id=uid(),
                            story_id=story.id,
                            actor_id=c.id,
                            kind="goal",
                            content=goal,
                            source_ids=[event.id],
                        )
                    )
            runtime.emit(
                s,
                c,
                "character_started",
                {
                    "card_id": cid,
                    "version": body.version,
                    "request_id": body.request_id,
                    "requested_story_id": body.story_id,
                },
            )
        return {"id": c.id, "card_id": cid, "version": body.version, "story_id": story.id}

    return router
