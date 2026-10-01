"""Local creator controls and player presence; all routes stay inside the role workspace."""

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, tuple_

from .agreements import DecisionInput, ProposalInput
from .observations import append_record, audience, members, notices, perceive, record_data, visible_records
from .schemas import CharacterRelationship, ObjectAction, SessionProfile
from .storage import (
    Conversation,
    ExperimentInitial,
    Observation,
    RoleMemory,
    RoleSnapshot,
    SceneRecord,
    Story,
    Task,
    now,
    uid,
)


class SceneSettings(BaseModel):
    scene_enabled: bool = True
    paused: bool = False
    idle_enabled: bool = True
    cooldown_seconds: int = Field(default=30, ge=10, le=600)
    chain_limit: int = Field(default=3, ge=1, le=3)


class Presence(BaseModel):
    visible: bool
    typing: bool
    voice_enabled: bool = False
    client_id: str | None = Field(default=None, min_length=1, max_length=80)


def opening_awaits_player(s, story, player):
    """Do not let an idle NPC answer its own authored opening question."""
    state = story.settings.get("narrative", {})
    if state.get("mode") != "player" or state.get("execution", {}).get("phase") != "interactive":
        return False
    origin = s.get(ExperimentInitial, story.id)
    graph = (origin.recipe if origin else {}).get("narrative") or {}
    node = next((n for n in graph.get("nodes", []) if n["id"] == state.get("node")), None)
    opening = (node or {}).get("opening") or []
    if not opening:
        return False
    last = opening[-1]
    if last.get("kind") != "dialogue" or not any(mark in last.get("text", "") for mark in ("?", "？")):
        return False
    if last.get("speaker") == origin.recipe.get("player", {}).get("alias"):
        return False
    entry = s.get(SceneRecord, state.get("event_id"))
    if not entry or entry.seq is None:
        return False
    return not s.scalar(
        select(SceneRecord.id)
        .where(
            SceneRecord.story_id == story.id,
            SceneRecord.actor_id == player.id,
            SceneRecord.kind == "speech",
            SceneRecord.seq > entry.seq,
        )
        .limit(1)
    )


def pending_player_reply(s, player):
    from .event_projection import requests_for

    return any(
        request["target_id"] == player.id and request["status"] in {"open", "partial", "defer"}
        for request in requests_for(s, player.id)
    )


class StepInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    actor_id: str


class StepPresence(BaseModel):
    visible: bool


class SnapshotInput(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    actor_id: str


class MemoryInput(BaseModel):
    kind: str = Field(pattern="^(belief|impression|commitment|goal|relationship)$")
    content: str = Field(min_length=1, max_length=800)
    request_id: str | None = Field(default=None, min_length=1, max_length=80)


class MemoryStatus(BaseModel):
    expected_version: int
    status: str = Field(pattern="^(active|archived|completed)$")


class Location(BaseModel):
    location: str = Field(min_length=1, max_length=100)
    request_id: str | None = Field(default=None, min_length=1, max_length=80)
    expected_revision: int | None = Field(default=None, ge=0)


class RelationshipChange(CharacterRelationship):
    expected_revision: int = Field(ge=1)


class ObjectCreate(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=100)
    holder_id: str | None = None
    location: str = Field(default="main", min_length=1, max_length=100)


class PlayerObjectAction(ObjectAction):
    request_id: str = Field(min_length=1, max_length=80)


class ObserveRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)


class ExchangeUpdate(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    status: str = Field(pattern="^(cancelled|open)$")


class HeardSpeech(BaseModel):
    actor_id: str
    text: str | None = Field(default=None, min_length=1, max_length=8000)
    speaker_known: bool = True


class AwarenessChange(BaseModel):
    noticed: bool
    expected_revision: int = Field(ge=0)


class AuthoredSpeech(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    actor_id: str
    text: str = Field(min_length=1, max_length=8000)
    channel: Literal["public", "private"] = "public"
    addressees: list[str] = Field(default_factory=list, max_length=30)
    observations: list[HeardSpeech] = Field(default_factory=list, max_length=30)
    expects_reply: bool = False
    target_id: str | None = None


class AuthoredReply(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    question_id: str
    actor_id: str
    response_kind: Literal["answer", "defer", "refuse", "silence"]
    text: str = Field(min_length=1, max_length=8000)
    channel: Literal["public", "private"] = "public"
    addressees: list[str] = Field(default_factory=list, max_length=30)
    observations: list[HeardSpeech] = Field(default_factory=list, max_length=30)


def role_router(db, config, runtime, owner):
    router = APIRouter(prefix="/api/role")

    def who(value=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return value

    def story(s, sid, identity):
        row = s.scalar(select(Story).where(Story.id == sid, Story.owner_id == identity).with_for_update())
        if not row:
            raise KeyError("剧情不存在")
        return row

    from .reading_api import reading_router

    router.include_router(reading_router(db, who))

    def actor(s, aid, identity):
        c = s.get(Conversation, aid)
        if not c or c.owner_id != identity or not c.story_id or not c.current_state.get("profile"):
            raise KeyError("角色不存在")
        return c

    def invalidate(s, story_id):
        from .scene_control import invalidate_scene

        invalidate_scene(s, story_id, runtime)

    @router.get("/players/{aid}/snapshot")
    def player_frame(aid: str, anchor: str | None = None, audio: bool = True, identity=Depends(who)):
        from .player_snapshot import player_snapshot

        with db.session() as s:
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            player = s.scalar(
                select(Conversation).where(Conversation.id == aid, Conversation.owner_id == identity)
            )
            if not player or not player.story_id or not player.current_state.get("player"):
                raise HTTPException(404, "玩家会话不存在。")
            row = s.get(Story, player.story_id)
            if not row or row.owner_id != identity:
                raise HTTPException(404, "故事不存在。")
            settings = SceneSettings(
                **{k: v for k, v in row.settings.items() if k in SceneSettings.model_fields}
            )
            return player_snapshot(s, player, row, settings.model_dump(), anchor, audio=audio)

    @router.get("/stories/{sid}/roster")
    def roster(sid: str, actor_id: str | None = None, identity=Depends(who)):
        with db.session() as s:
            story(s, sid, identity)
            viewer = actor(s, actor_id, identity) if actor_id else None
            if viewer and viewer.story_id != sid:
                raise ValueError("角色不属于此剧情。")
            return [
                {
                    "id": c.id,
                    "name": c.current_state["profile"]["name"],
                    "player": bool(c.current_state.get("player")),
                    "location": c.current_state.get("location", "main"),
                    "revision": c.revision,
                    **({"unnoticed_ids": c.current_state.get("unnoticed_ids", [])} if viewer is None else {}),
                }
                for c in members(s, sid)
                if viewer is None
                or (
                    notices(viewer, c)
                    and c.current_state.get("location", "main")
                    == viewer.current_state.get("location", "main")
                )
            ]

    @router.get("/stories/{sid}/observation-frame")
    def experiment_frame(sid: str, actor_id: str | None = None, identity=Depends(who)):
        from .experiment_views import observation_frame

        with db.session() as s:
            # One database snapshot; background memory work cannot split the two views.
            s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            return observation_frame(s, sid, identity, actor_id)

    @router.get("/stories/{sid}/timeline")
    def timeline(
        sid: str,
        actor_id: str | None = None,
        before: int | None = None,
        stage: bool = False,
        identity=Depends(who),
    ):
        from .items.stage import handoff_stage

        with db.session() as s:
            story(s, sid, identity)
            q = select(SceneRecord).where(
                SceneRecord.story_id == sid,
                SceneRecord.redacted.is_(False),
                SceneRecord.seq.is_not(None),
            )
            if actor_id:
                if actor(s, actor_id, identity).story_id != sid:
                    raise ValueError("角色不属于此剧情。")
                q = q.join(Observation).where(Observation.actor_id == actor_id)
            if before is not None:
                q = q.where(SceneRecord.seq < before)
            rows = list(s.scalars(q.order_by(SceneRecord.seq.desc()).limit(100)))
            return {
                "events": [
                    {
                        **record_data(
                            perceive(e, s.get(Observation, (actor_id, e.id))) if actor_id else e,
                            privileged=actor_id is None,
                            viewer_id=actor_id,
                        ),
                        **(
                            handoff_stage(s, perceive(e, s.get(Observation, (actor_id, e.id))), actor_id)
                            if stage and actor_id
                            else {}
                        ),
                        **(
                            {
                                "observers": list(
                                    s.scalars(
                                        select(Observation.actor_id).where(Observation.event_id == e.id)
                                    )
                                )
                            }
                            if actor_id is None
                            else {}
                        ),
                    }
                    for e in reversed(rows)
                ],
                "next_before": rows[-1].seq if len(rows) == 100 else None,
            }

    @router.patch("/actors/{aid}/awareness/{target_id}")
    def awareness(aid: str, target_id: str, body: AwarenessChange, identity=Depends(who)):
        with db.session.begin() as s:
            observer = actor(s, aid, identity)
            story(s, observer.story_id, identity)
            target = actor(s, target_id, identity)
            s.refresh(observer, with_for_update=True)
            if observer.story_id != target.story_id or aid == target_id:
                raise ValueError("只能调整同剧情其他角色的察觉关系。")
            if observer.revision != body.expected_revision:
                raise HTTPException(409, "角色状态已变化，请刷新后重试。")
            hidden = set(observer.current_state.get("unnoticed_ids", []))
            hidden.discard(target_id) if body.noticed else hidden.add(target_id)
            observer.current_state = {**observer.current_state, "unnoticed_ids": sorted(hidden)}
            invalidate(s, observer.story_id)
            return {"ok": True, "revision": observer.revision}

    @router.post("/stories/{sid}/speech-events", status_code=201)
    def authored_speech(sid: str, body: AuthoredSpeech, identity=Depends(who)):
        """Creator control for reproducible hearing scenarios; never a confirmed world fact."""
        with db.session.begin() as s:
            story(s, sid, identity)
            speaker = actor(s, body.actor_id, identity)
            if speaker.story_id != sid:
                raise ValueError("发言者不属于此剧情。")
            dedupe = f"authored-speech:{body.request_id}"
            supplied = hashlib.sha256(
                json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            old = s.scalar(
                select(SceneRecord).where(SceneRecord.story_id == sid, SceneRecord.dedupe == dedupe)
            )
            if old:
                if old.redacted or old.payload.get("authoring_request_hash") != supplied:
                    raise HTTPException(409, "重复请求与原发言不一致")
                return {"id": old.id}
            allowed = set(audience(s, speaker, body.channel, body.addressees))
            observer_ids = [o.actor_id for o in body.observations]
            if len(set(observer_ids)) != len(observer_ids) or not set(observer_ids).issubset(allowed):
                raise ValueError("观察者重复、不同场或无权接收此频道。")
            if body.expects_reply != bool(body.target_id):
                raise ValueError("期待回应时必须指定一位回应角色，普通发言不能携带回应目标。")
            if body.expects_reply:
                target_hearing = next(
                    (
                        observation
                        for observation in body.observations
                        if observation.actor_id == body.target_id
                    ),
                    None,
                )
                if (
                    body.target_id == speaker.id
                    or body.target_id not in allowed
                    or not target_hearing
                    or target_hearing.text not in {None, body.text}
                    or not target_hearing.speaker_known
                ):
                    raise ValueError("回应角色必须在当前交流范围内，并完整听到且认出这次提问。")
            perception = {
                o.actor_id: {
                    "text": o.text if o.text is not None else body.text,
                    "speaker_known": o.speaker_known,
                }
                for o in body.observations
            }
            event = append_record(
                s,
                story_id=sid,
                actor=speaker,
                kind="speech",
                text=body.text,
                observer_ids=sorted({speaker.id, *observer_ids}),
                channel=body.channel,
                addressees=body.addressees,
                dedupe=dedupe,
                perceptions=perception,
                payload={
                    "authoring_request_hash": supplied,
                    **(
                        {"exchange": {"expects_reply": True, "target_id": body.target_id}}
                        if body.expects_reply
                        else {}
                    ),
                },
            )
            invalidate(s, sid)
            return {"id": event.id}

    @router.post("/stories/{sid}/reply-events", status_code=201)
    def authored_reply(sid: str, body: AuthoredReply, identity=Depends(who)):
        """Creator-authored reply using the same event, perception and request projections as play."""
        from .event_projection import requests_for

        with db.session.begin() as s:
            story(s, sid, identity)
            speaker = actor(s, body.actor_id, identity)
            if speaker.story_id != sid:
                raise ValueError("回应者不属于此剧情。")
            dedupe = f"authored-reply:{body.request_id}"
            supplied = hashlib.sha256(
                json.dumps(body.model_dump(), sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            old = s.scalar(
                select(SceneRecord).where(SceneRecord.story_id == sid, SceneRecord.dedupe == dedupe)
            )
            if old:
                if old.redacted or old.payload.get("authoring_request_hash") != supplied:
                    raise HTTPException(409, "重复请求与原回应不一致")
                return {"id": old.id}
            item = next(
                (row for row in requests_for(s, speaker.id) if row["question_id"] == body.question_id),
                None,
            )
            if not item or item["target_id"] != speaker.id:
                raise HTTPException(404, "回应者没有完整获知这条指定给自己的请求")
            if item["status"] not in {"open", "partial", "defer"}:
                raise HTTPException(409, "此请求已经结束，不能再次回应")
            allowed = set(audience(s, speaker, body.channel, body.addressees))
            observer_ids = [observation.actor_id for observation in body.observations]
            if len(set(observer_ids)) != len(observer_ids) or not set(observer_ids).issubset(allowed):
                raise ValueError("观察者重复、不同场或无权接收此频道。")
            perception = {
                observation.actor_id: {
                    "text": observation.text if observation.text is not None else body.text,
                    "speaker_known": observation.speaker_known,
                }
                for observation in body.observations
            }
            event = append_record(
                s,
                story_id=sid,
                actor=speaker,
                kind="speech",
                text=body.text,
                observer_ids=sorted({speaker.id, *observer_ids}),
                channel=body.channel,
                addressees=body.addressees,
                parent_id=body.question_id,
                dedupe=dedupe,
                perceptions=perception,
                payload={
                    "authoring_request_hash": supplied,
                    "exchange": {
                        "reply_to": body.question_id,
                        "response_kind": body.response_kind,
                    },
                },
            )
            invalidate(s, sid)
            return {"id": event.id}

    @router.get("/actors/{aid}/memories")
    def memories(aid: str, query: str = "", before: str | None = None, kind: str = "", identity=Depends(who)):
        with db.session() as s:
            c = actor(s, aid, identity)
            valid = {e.id for e in visible_records(s, aid)}
            q = select(RoleMemory).where(RoleMemory.actor_id == c.id, RoleMemory.status != "deleted")
            if kind:
                if kind not in {"episode", "belief", "impression", "commitment", "goal", "relationship"}:
                    raise ValueError("不支持的记忆类别")
                q = q.where(RoleMemory.kind == kind)
            if query:
                q = q.where(RoleMemory.content.contains(query[:100]))
            if before:
                old = s.get(RoleMemory, before)
                if not old or old.actor_id != aid:
                    raise ValueError("无效记忆游标")
                q = q.where(tuple_(RoleMemory.created_at, RoleMemory.id) < (old.created_at, old.id))
            rows = list(s.scalars(q.order_by(RoleMemory.created_at.desc(), RoleMemory.id.desc()).limit(100)))
            return {
                "memories": [
                    {
                        "id": m.id,
                        "kind": m.kind,
                        "content": m.content,
                        "source_ids": m.source_ids,
                        "status": m.status,
                        "version": m.version,
                        "created_at": m.created_at,
                    }
                    for m in rows
                    if set(m.source_ids).issubset(valid)
                ],
                "next_before": rows[-1].id if len(rows) == 100 else None,
            }

    @router.get("/actors/{aid}/knowledge")
    def knowledge(aid: str, key: str, identity=Depends(who)):
        from .observations import current_knowledge

        with db.session() as s:
            c = actor(s, aid, identity)
            facts, claims = current_knowledge(c, visible_records(s, aid))
            return {"fact": facts.get(key), "claims": [v for v in claims.values() if v["key"] == key]}

    @router.post("/actors/{aid}/memories", status_code=201)
    def add_memory(aid: str, body: MemoryInput, identity=Depends(who)):
        with db.session.begin() as s:
            c = actor(s, aid, identity)
            story(s, c.story_id, identity)
            dedupe = f"memory-authoring:{aid}:{body.request_id}" if body.request_id else uid()
            prior = s.scalar(
                select(SceneRecord).where(SceneRecord.story_id == c.story_id, SceneRecord.dedupe == dedupe)
            )
            if prior:
                if prior.redacted or prior.text != body.content or prior.payload.get("kind") != body.kind:
                    raise HTTPException(409, "同一请求与原个人条目不一致")
                memory = s.scalar(
                    select(RoleMemory).where(
                        RoleMemory.actor_id == aid,
                        RoleMemory.kind == body.kind,
                        RoleMemory.source_ids.contains([prior.id]),
                        RoleMemory.status != "deleted",
                    )
                )
                if not memory:
                    raise HTTPException(409, "原个人条目已不可用，请刷新核对")
                return {"id": memory.id}
            count = len(
                list(
                    s.scalars(
                        select(RoleMemory.id).where(
                            RoleMemory.actor_id == aid,
                            RoleMemory.kind != "episode",
                            RoleMemory.status == "active",
                        )
                    )
                )
            )
            if count >= 30:
                raise ValueError("活跃目标与个人条目较多，请先完成或归档部分条目。")
            event = append_record(
                s,
                story_id=c.story_id,
                kind="authoring",
                text=body.content,
                observer_ids=[aid],
                channel="private",
                dedupe=dedupe,
                payload={"kind": body.kind},
            )
            memory = RoleMemory(
                id=uid(),
                story_id=c.story_id,
                actor_id=aid,
                kind=body.kind,
                content=body.content,
                source_ids=[event.id],
            )
            s.add(memory)
            invalidate(s, c.story_id)
            return {"id": memory.id}

    @router.patch("/actors/{aid}/memories/{mid}")
    def update_memory(aid: str, mid: str, body: MemoryStatus, identity=Depends(who)):
        with db.session.begin() as s:
            c = actor(s, aid, identity)
            story(s, c.story_id, identity)
            row = s.scalar(
                select(RoleMemory).where(RoleMemory.id == mid, RoleMemory.actor_id == aid).with_for_update()
            )
            if not row:
                raise KeyError("记忆不存在")
            if row.version != body.expected_version:
                raise HTTPException(409, "记忆已变化，请刷新后重试。")
            if row.kind == "episode":
                raise ValueError("原始经历请通过事件删除处理，不能伪装成已完成目标。")
            row.status, row.version = body.status, row.version + 1
            append_record(
                s,
                story_id=c.story_id,
                kind="memory_change",
                text=f"{row.kind}：{row.content} → {body.status}",
                observer_ids=[aid],
                channel="private",
                dedupe=f"memory:{mid}:{row.version}",
                payload={"memory_id": mid, "status": body.status},
            )
            invalidate(s, c.story_id)
            return {"version": row.version}

    @router.post("/actors/{aid}/reflect")
    def reflect(aid: str, identity=Depends(who)):
        from .role_memory import RoleMemoryService

        with db.session() as s:
            actor(s, aid, identity)
        task = RoleMemoryService(db, config, runtime, None).schedule(aid, identity, force=True)
        return {"task_id": task.id if task else None}

    @router.patch("/actors/{aid}/location")
    def location(aid: str, body: Location, identity=Depends(who)):
        if not body.location.strip():
            raise ValueError("场景名称不能为空。")
        with db.session.begin() as s:
            c = actor(s, aid, identity)
            story(s, c.story_id, identity)
            s.refresh(c, with_for_update=True)
            dedupe = f"location-authoring:{aid}:{body.request_id}" if body.request_id else uid()
            prior = s.scalar(
                select(SceneRecord).where(SceneRecord.story_id == c.story_id, SceneRecord.dedupe == dedupe)
            )
            if prior:
                if prior.redacted or prior.payload.get("to") != body.location.strip():
                    raise HTTPException(409, "同一请求与原场景变更不一致")
                return {"ok": True}
            if body.expected_revision is not None and c.revision != body.expected_revision:
                raise HTTPException(409, "角色状态已变化，请刷新后重试")
            previous = c.current_state.get("location", "main")
            observers = [p.id for p in members(s, c.story_id) if p.id in audience(s, c) and notices(p, c)]
            c.current_state = {**c.current_state, "location": body.location.strip()}
            observers = sorted(
                set(
                    observers
                    + [p.id for p in members(s, c.story_id) if p.id in audience(s, c) and notices(p, c)]
                )
            )
            append_record(
                s,
                story_id=c.story_id,
                kind="scene_change",
                actor=c,
                text=f"主持人将{c.current_state['profile']['name']}从{previous}移至{body.location.strip()}",
                observer_ids=observers,
                dedupe=dedupe,
                payload={"from": previous, "to": body.location.strip()},
            )
            invalidate(s, c.story_id)
            return {"ok": True}

    @router.post("/actors/{aid}/relationships", status_code=201)
    def relationship(aid: str, body: RelationshipChange, identity=Depends(who)):
        with db.session.begin() as s:
            c = actor(s, aid, identity)
            story(s, c.story_id, identity)
            s.refresh(c, with_for_update=True)
            if c.revision != body.expected_revision:
                raise HTTPException(409, "角色状态已变化，请刷新后重试。")
            if not body.target.strip() or not body.relation.strip() or not body.basis.strip():
                raise ValueError("请填写关系对象、关系和变更依据。")
            value = body.model_dump(exclude={"expected_revision"})
            profile = dict(c.current_state["profile"])
            relations = profile.get("relationships", [])
            previous = [r for r in relations if r["target"] == body.target]
            profile["relationships"] = [r for r in relations if r["target"] != body.target] + [value]
            profile = SessionProfile(**profile).model_dump()
            c.current_state = {**c.current_state, "profile": profile}
            prior_events = [
                r
                for r in visible_records(s, aid)
                if r.kind == "relationship" and r.payload.get("current", {}).get("target") == body.target
            ]
            event = append_record(
                s,
                story_id=c.story_id,
                kind="relationship",
                text=f"与{body.target}的关系变为{body.relation}（{body.status}）；依据：{body.basis}",
                observer_ids=[aid],
                channel="private",
                dedupe=uid(),
                parent_id=prior_events[-1].id if prior_events else None,
                payload={"previous": previous, "current": value},
            )
            prior_ids = {record.id for record in prior_events}
            replaced = []
            for memory in s.scalars(
                select(RoleMemory).where(
                    RoleMemory.actor_id == aid,
                    RoleMemory.kind == "relationship",
                    RoleMemory.status == "active",
                )
            ):
                if prior_ids.intersection(memory.source_ids):
                    memory.status = "archived"
                    memory.version += 1
                    replaced.append(memory)
            s.add(
                RoleMemory(
                    id=uid(),
                    story_id=c.story_id,
                    actor_id=aid,
                    kind="relationship",
                    content=event.text,
                    source_ids=[event.id],
                    replaces_id=replaced[-1].id if replaced else None,
                )
            )
            invalidate(s, c.story_id)
            return {"id": event.id, "revision": c.revision}

    @router.get("/stories/{sid}/settings")
    def settings(sid: str, identity=Depends(who)):
        with db.session() as s:
            row = story(s, sid, identity)
            return SceneSettings(**{k: v for k, v in row.settings.items() if k in SceneSettings.model_fields})

    @router.put("/stories/{sid}/settings")
    async def set_settings(sid: str, body: SceneSettings, identity=Depends(who)):
        if not body.paused:
            await runtime.model_readiness.require()
        with db.session.begin() as s:
            row = story(s, sid, identity)
            if row.settings.get("narrative", {}).get("mode") == "backstage" and not body.paused:
                raise HTTPException(409, "幕后场景采用暂停单步模式，请在实验台推进或由作者结束场景。")
            resuming = bool(row.settings.get("paused")) and not body.paused
            row.settings = {**row.settings, **body.model_dump()}
            invalidate(s, sid)
            if resuming:
                until = now().timestamp() + body.cooldown_seconds
                for member in members(s, sid):
                    if member.current_state.get("player"):
                        member.current_state = {
                            **member.current_state,
                            "resume_grace_until": until,
                        }
        return body

    @router.post("/stories/{sid}/presence")
    def presence(sid: str, body: Presence, identity=Depends(who)):
        with db.session.begin() as s:
            row = story(s, sid, identity)
            people = members(s, sid)
            player = next((c for c in people if c.current_state.get("player")), None)
            if not player:
                return {"task_id": None}
            s.refresh(player, with_for_update=True)
            tick = now().timestamp()
            from .player_presence import merge_presence

            clients, combined = merge_presence(
                player.current_state.get("presence_clients", {}),
                body.client_id,
                body.visible,
                body.typing,
                tick,
            )
            row.settings = {**row.settings, "activity_until": combined["until"] if combined["visible"] else 0}
            state = {**player.current_state, "presence_clients": clients, "presence": combined}
            state.setdefault("last_input_at", tick)
            player.current_state = state
            settings = SceneSettings(**row.settings)
            conditions = [
                (bool(row.settings.get("narrative", {}).get("finished_at")), "finished"),
                (not combined["visible"], "hidden"),
                (combined["typing"], "player_input"),
                (settings.paused, "paused"),
                (row.settings.get("auto_route", {}).get("source_seq") == row.seq, "scene_transition"),
                (row.settings.get("narrative", {}).get("execution", {}).get("phase") in {"opening", "video"}, "opening"),
                (opening_awaits_player(s, row, player), "opening_question"),
                (pending_player_reply(s, player), "pending_player_reply"),
                (not settings.idle_enabled, "disabled"),
                (state.get("idle_issued"), "waiting_event"),
                (tick < state.get("resume_grace_until", 0), "resume_cooldown"),
                (tick - state.get("last_input_at", tick) < settings.cooldown_seconds, "cooldown"),
            ]
            for blocked, reason in conditions:
                if blocked:
                    return {"task_id": None, "reason": reason}
            if s.scalar(
                select(Task.id)
                .where(
                    Task.session_id == player.id,
                    Task.kind == "respond",
                    Task.status.in_(["queued", "running", "waiting"]),
                )
                .limit(1)
            ):
                return {"task_id": None, "reason": "active_turn"}
            latest = s.scalar(
                select(Task)
                .where(Task.session_id == player.id, Task.kind == "respond", Task.status == "completed")
                .order_by(Task.created_at.desc(), Task.id.desc())
                .limit(1)
            )
            handoff = (latest.result or {}).get("director_handoff") if latest else None
            narrative = row.settings.get("narrative", {})
            if handoff and handoff == {
                "world_seq": row.seq,
                "node_id": narrative.get("node"),
                "entry_event_id": narrative.get("event_id", ""),
                "epoch": player.epoch,
            }:
                return {"task_id": None, "reason": "director_handoff"}
            observers = audience(s, player)
            goals = list(
                s.scalars(
                    select(RoleMemory)
                    .where(
                        RoleMemory.actor_id.in_(observers),
                        RoleMemory.actor_id != player.id,
                        RoleMemory.kind.in_(["goal", "commitment"]),
                        RoleMemory.status == "active",
                    )
                    .limit(8)
                )
            )
            if not goals:
                return {"task_id": None, "reason": "no_goal"}
            player.epoch += 1
            player.current_state = {**state, "idle_issued": True}
            task = runtime.enqueue(
                s,
                "respond",
                identity,
                {
                    "text": "根据尚未完成的目标决定是否主动交流；没有必要可以等待。",
                    "mode": "auto",
                    "autonomous": True,
                    "voice_enabled": body.voice_enabled,
                    "audience": observers,
                    "channel": "public",
                    "addressees": [],
                },
                session_id=player.id,
                epoch=player.epoch,
                priority=5,
            )
            runtime.emit(s, player, "autonomous_started", {}, task.id)
            return {"task_id": task.id}

    @router.delete("/stories/{sid}/events/{eid}")
    def redact(sid: str, eid: str, identity=Depends(who)):
        with db.session.begin() as s:
            story(s, sid, identity)
            event = s.get(SceneRecord, eid)
            if not event or event.story_id != sid:
                raise KeyError("事件不存在")
            records = list(s.scalars(select(SceneRecord).where(SceneRecord.story_id == sid)))
            deleted = {eid}
            host_ids = (
                {event.payload["event_id"]}
                if event.kind == "host" and event.payload.get("event_id")
                else set()
            )
            while True:
                expanded = deleted | {
                    r.id
                    for r in records
                    if r.parent_id in deleted
                    or (r.kind == "host" and r.payload.get("event_id") in host_ids)
                    or any(
                        claim.get("source_id") in deleted | host_ids for claim in r.payload.get("claims", [])
                    )
                    or deleted.intersection(r.payload.get("context_source_ids", []))
                }
                if expanded == deleted:
                    break
                deleted = expanded
            task_ids = {r.task_id for r in records if r.id in deleted and r.task_id}
            relationship_targets = {}
            for record in records:
                if record.id in deleted and record.kind == "relationship":
                    for observer in s.scalars(
                        select(Observation.actor_id).where(Observation.event_id == record.id)
                    ):
                        relationship_targets.setdefault(observer, set()).add(
                            record.payload["current"]["target"]
                        )
            for record in records:
                if record.id in deleted:
                    object_id = (record.payload.get("object") or {}).get("id")
                    record.redacted, record.text, record.payload = (
                        True,
                        "[已删除]",
                        ({"object": {"id": object_id}} if object_id else {}),
                    )
            for observation in s.scalars(select(Observation).where(Observation.event_id.in_(deleted))):
                observation.perception = {}
            for c in members(s, sid):
                c.privacy_version += 1
                state = dict(c.current_state)
                if c.id in relationship_targets:
                    state["profile"] = {
                        **state["profile"],
                        "relationships": [
                            r
                            for r in state["profile"].get("relationships", [])
                            if r["target"] not in relationship_targets[c.id]
                        ],
                    }
                state.pop("latest_user_message", None)
                state["scene_facts"] = {
                    k: v for k, v in state.get("scene_facts", {}).items() if v.get("event_id") not in host_ids
                }
                state["scene_claims"] = {
                    k: v
                    for k, v in state.get("scene_claims", {}).items()
                    if v.get("event_id") not in host_ids
                }
                c.current_state = state
            for m in s.scalars(select(RoleMemory).where(RoleMemory.story_id == sid)):
                if deleted.intersection(m.source_ids):
                    m.status, m.content = "deleted", "[来源已删除]"
            # Suppress old aggregate messages and snapshots as well; no resurrection via another reader.
            if task_ids:
                from .storage import Event, Message, Operation

                for message in s.scalars(
                    select(Message).where((Message.task_id.in_(task_ids)) | (Message.id.in_(deleted)))
                ):
                    message.redacted, message.content = True, "[相关事件已删除]"
                for operation in s.scalars(select(Operation).where(Operation.task_id.in_(task_ids))):
                    operation.payload = {"redacted": True}
                for old_event in s.scalars(select(Event).where(Event.task_id.in_(task_ids))):
                    old_event.payload = {"redacted": True}
                for task in s.scalars(select(Task).where(Task.id.in_(task_ids))):
                    task.result = {"redacted": True}
                    task.payload = {**task.payload, "text": "[已删除]"}
            if host_ids:
                from .storage import StoryEvent

                for host_event in s.scalars(select(StoryEvent).where(StoryEvent.id.in_(host_ids))):
                    host_event.payload = {
                        "request": {"key": "[已删除]", "value": "[已删除]", "recipients": []}
                    }
            invalidate(s, sid)
            for c in members(s, sid):
                runtime.emit(s, c, "privacy_reset", {})
        return {"ok": True}

    @router.get("/stories/{sid}/objects")
    def list_objects(sid: str, actor_id: str | None = None, identity=Depends(who)):
        from .scene_state import objects

        with db.session() as s:
            story(s, sid, identity)
            if actor_id and actor(s, actor_id, identity).story_id != sid:
                raise ValueError("角色不属于此剧情。")
            return objects(s, sid, actor_id)

    @router.post("/stories/{sid}/objects", status_code=201)
    def create_object(sid: str, body: ObjectCreate, identity=Depends(who)):
        with db.session.begin() as s:
            story(s, sid, identity)
            dedupe = f"object-create:{body.request_id}"
            old = s.scalar(
                select(SceneRecord).where(SceneRecord.story_id == sid, SceneRecord.dedupe == dedupe)
            )
            if old:
                if old.redacted:
                    raise ValueError("该请求的物品已删除。")
                if old.payload.get("request") != body.model_dump():
                    raise ValueError("同一请求ID不能创建不同物品。")
                return old.payload["object"]
            people = {p.id: p for p in members(s, sid)}
            from .scene_state import objects

            if len(objects(s, sid)) >= 30:
                raise ValueError("首版每个剧情最多登记30件关键物品。")
            holder = people.get(body.holder_id) if body.holder_id else None
            if body.holder_id and not holder:
                raise ValueError("持有人不属于此剧情。")
            location = holder.current_state.get("location", "main") if holder else body.location
            visible = [
                p.id
                for p in people.values()
                if p.current_state.get("location", "main") == location and (not holder or notices(p, holder))
            ]
            item = {
                "id": uid(),
                "name": body.name,
                "holder_id": body.holder_id,
                "location": location,
                "version": 1,
            }
            invalidate(s, sid)
            append_record(
                s,
                story_id=sid,
                kind="object_state",
                text=f"登记关键物品：{body.name}。",
                observer_ids=visible,
                dedupe=dedupe,
                payload={"object": item, "object_record_role": "mutation", "request": body.model_dump()},
            )
            return item

    @router.post("/actors/{aid}/object-actions")
    def player_object_action(aid: str, body: PlayerObjectAction, identity=Depends(who)):
        from .scene_state import transition

        with db.session.begin() as s:
            person = actor(s, aid, identity)
            story(s, person.story_id, identity)
            if not person.current_state.get("player"):
                raise ValueError("此操作入口仅供玩家控制自己的行动。")
            dedupe = f"player-object:{aid}:{body.request_id}"
            old = s.scalar(
                select(SceneRecord).where(
                    SceneRecord.story_id == person.story_id, SceneRecord.dedupe == dedupe
                )
            )
            if old:
                if old.redacted or old.payload.get("request") != body.model_dump():
                    raise ValueError("重复请求与原行动不一致或已删除。")
                return old.payload["object"]
            from .scene_execution import story_finished

            if story_finished(s.get(Story, person.story_id)):
                raise HTTPException(409, "故事已结束，不能再进行物品操作。")
            state, text, observers, source = transition(s, person, body.model_dump())
            invalidate(s, person.story_id)
            append_record(
                s,
                story_id=person.story_id,
                kind="object_state",
                actor=person,
                text=text,
                observer_ids=observers,
                dedupe=dedupe,
                payload={
                    "object": state,
                    "object_record_role": "mutation",
                    "request": body.model_dump(),
                    "context_source_ids": [source],
                    "agreement": {"type": "completion", **body.agreement.model_dump()}
                    if body.agreement
                    else None,
                },
            )
            return state

    @router.get("/stories/{sid}/exchanges")
    def list_exchanges(sid: str, actor_id: str, identity=Depends(who)):
        from .event_projection import requests_for

        with db.session() as s:
            story(s, sid, identity)
            if actor(s, actor_id, identity).story_id != sid:
                raise ValueError("角色不属于此剧情。")
            return requests_for(s, actor_id)

    @router.post("/actors/{aid}/observe")
    def observe(aid: str, body: ObserveRequest, identity=Depends(who)):
        from .scene_state import objects, observe_scene

        with db.session.begin() as s:
            person = actor(s, aid, identity)
            story(s, person.story_id, identity)
            if observe_scene(s, person, body.request_id):
                invalidate(s, person.story_id)
            return objects(s, person.story_id, aid)

    @router.post("/actors/{aid}/exchanges/{eid}")
    def update_exchange(aid: str, eid: str, body: ExchangeUpdate, identity=Depends(who)):
        from .event_projection import requests_for

        with db.session.begin() as s:
            c = actor(s, aid, identity)
            story(s, c.story_id, identity)
            dedupe = f"exchange-update:{aid}:{body.request_id}"
            change = {"question_id": eid, "status": body.status}
            old = s.scalar(
                select(SceneRecord).where(SceneRecord.story_id == c.story_id, SceneRecord.dedupe == dedupe)
            )
            if old:
                if old.redacted or old.payload.get("exchange_update") != change:
                    raise HTTPException(409, "重复请求与原事项更改不一致")
                return {"ok": True}
            original = s.get(SceneRecord, eid)
            rows = {r["question_id"]: r for r in requests_for(s, aid)}
            item = rows.get(eid)
            if not item or item["asker_id"] != aid or not c.current_state.get("player"):
                raise HTTPException(404, "只能更改自己提出的事项")
            if item["status"] not in {"open", "partial", "defer", "cancelled"}:
                raise HTTPException(409, "此事项已得到回应")
            observers = list(s.scalars(select(Observation.actor_id).where(Observation.event_id == eid)))
            append_record(
                s,
                story_id=c.story_id,
                kind="exchange_update",
                actor=c,
                text="撤回此请求。" if body.status == "cancelled" else "重新提出此请求。",
                observer_ids=observers,
                channel=original.channel,
                parent_id=eid,
                dedupe=dedupe,
                payload={"exchange_update": change},
            )
            invalidate(s, c.story_id)
            if body.status == "open":
                target = s.get(Conversation, item["target_id"])
                if not target or target.current_state.get("location", "main") != c.current_state.get(
                    "location", "main"
                ):
                    raise HTTPException(409, "对方不在当前场景，无法继续请求")
                task = runtime.enqueue(
                    s,
                    "respond",
                    identity,
                    {
                        "text": "继续处理此前未完成的请求：" + original.text,
                        "mode": "auto",
                        "audience": audience(s, c, original.channel, [target.id]),
                        "channel": original.channel,
                        "addressees": [target.id],
                        "event_id": eid,
                        "resume_event_id": eid,
                    },
                    session_id=aid,
                    epoch=c.epoch,
                    priority=5,
                )
                runtime.emit(s, c, "accepted", {"message": "继续处理此前请求"}, task.id)
        return {"ok": True}

    @router.get("/stories/{sid}/agreements")
    def list_agreements(sid: str, actor_id: str, identity=Depends(who)):
        from .agreements import proposals

        with db.session() as s:
            story(s, sid, identity)
            if actor(s, actor_id, identity).story_id != sid:
                raise ValueError("角色不属于此剧情")
            return proposals(s, sid, actor_id)

    @router.post("/actors/{aid}/agreements")
    def create_agreement(aid: str, body: ProposalInput, identity=Depends(who)):
        from .agreements import propose

        with db.session.begin() as s:
            person = actor(s, aid, identity)
            story(s, person.story_id, identity)
            before = s.get(Story, person.story_id).seq
            event = propose(s, person, body)
            if event.seq > before:
                invalidate(s, person.story_id)
            return {"event_id": event.id, **event.payload["agreement"]}

    @router.post("/actors/{aid}/agreements/{pid}/decisions")
    def agreement_decision(aid: str, pid: str, body: DecisionInput, identity=Depends(who)):
        from .agreements import decide

        with db.session.begin() as s:
            person = actor(s, aid, identity)
            story(s, person.story_id, identity)
            before = s.get(Story, person.story_id).seq
            event = decide(s, person, pid, body)
            if event.seq > before:
                invalidate(s, person.story_id)
            return {"event_id": event.id, **event.payload["agreement"]}

    @router.get("/stories/{sid}/timing-overview")
    def timing_overview(sid: str, identity=Depends(who)):
        from .story_timing import story_timing

        with db.session() as s:
            return story_timing(s, story(s, sid, identity))

    @router.get("/stories/{sid}/traces")
    def trace_tasks(sid: str, identity=Depends(who)):
        with db.session() as s:
            story(s, sid, identity)
            rows = s.scalars(
                select(Task)
                .outerjoin(Conversation, Conversation.id == Task.session_id)
                .where(
                    Task.owner_id == identity,
                    Task.kind.in_(["respond", "scene_run", "npc_step"]),
                    (Conversation.story_id == sid) | (Task.payload["story_id"].as_string() == sid),
                )
                .order_by(Task.created_at.desc())
                .limit(30)
            )
            return [
                {"id": t.id, "kind": t.kind, "status": t.status, "created_at": t.created_at} for t in rows
            ]

    @router.get("/stories/{sid}/tasks/{tid}/trace")
    def inspect_behavior(sid: str, tid: str, identity=Depends(who)):
        from .behavior_trace import behavior_trace

        with db.session() as s:
            story(s, sid, identity)
            task = s.get(Task, tid)
            person = s.get(Conversation, task.session_id) if task and task.session_id else None
            task_story = (
                task.payload.get("story_id")
                if task and task.kind in {"scene_run", "npc_step"}
                else person.story_id
                if person
                else None
            )
            if not task or task.owner_id != identity or task_story != sid:
                raise KeyError("行为记录不存在")
            return behavior_trace(s, task)

    @router.get("/stories/{sid}/activities")
    def activities(sid: str, identity=Depends(who)):
        with db.session() as s:
            story(s, sid, identity)
            rows = list(
                s.scalars(
                    select(Task)
                    .where(
                        Task.kind == "scene_run",
                        Task.owner_id == identity,
                        Task.payload["story_id"].as_string() == sid,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(30)
                )
            )
            return [
                {
                    "id": task.id,
                    "scene": task.payload["scene"],
                    "status": task.status,
                    "attempt": task.attempt,
                    "trigger": task.payload.get("trigger"),
                    "recovery": task.payload.get("recovery"),
                    "created_at": task.created_at,
                }
                for task in rows
            ]

    @router.post("/stories/{sid}/activities/{tid}/cancel")
    def cancel_activity(sid: str, tid: str, identity=Depends(who)):
        with db.session.begin() as s:
            story(s, sid, identity)
            task = s.get(Task, tid, with_for_update=True)
            if (
                not task
                or task.kind != "scene_run"
                or task.owner_id != identity
                or task.payload.get("story_id") != sid
            ):
                raise KeyError("场景活动不存在")
            if task.status in {"queued", "running", "waiting"}:
                task.status = "cancelled"
        return {"ok": True}

    @router.post("/stories/{sid}/steps", status_code=201)
    def step_create(sid: str, body: StepInput, identity=Depends(who)):
        from .experiments import create_step

        with db.session.begin() as s:
            row = story(s, sid, identity)
            task = create_step(s, row, body.actor_id, body.request_id, runtime)
            return {"task_id": task.id}

    def step_task(s, sid, tid, identity):
        task = s.get(Task, tid, with_for_update=True)
        if (
            not task
            or task.kind != "npc_step"
            or task.owner_id != identity
            or task.payload.get("story_id") != sid
        ):
            raise HTTPException(404, "实验任务不存在。")
        return task

    @router.get("/stories/{sid}/steps/{tid}")
    def step_get(sid: str, tid: str, identity=Depends(who)):
        with db.session() as s:
            story(s, sid, identity)
            task = step_task(s, sid, tid, identity)
            records = s.scalars(
                select(SceneRecord)
                .where(
                    SceneRecord.task_id == tid,
                    SceneRecord.redacted.is_(False),
                    SceneRecord.seq.is_not(None),
                )
                .order_by(SceneRecord.seq)
            )
            return {
                "task_id": task.id,
                "status": task.status,
                "error": task.error,
                "outcome": task.result.get("experiment", {}).get("outcome"),
                "stop_reason": task.result.get("stop_reason"),
                "actor_id": task.payload["step_actor_id"],
                "events": [
                    {
                        "id": r.id,
                        "kind": r.kind,
                        "text": r.text,
                        "actor_id": r.actor_id,
                        "item_action_status": (r.payload.get("item_action_result") or {}).get("status"),
                        "item_action_code": (r.payload.get("item_action_result") or {}).get("code"),
                    }
                    for r in records
                ],
            }

    @router.post("/stories/{sid}/steps/{tid}/presence")
    def step_presence(sid: str, tid: str, body: StepPresence, identity=Depends(who)):
        from .experiments import ACTIVE, valid_step

        with db.session.begin() as s:
            row = story(s, sid, identity)
            task = step_task(s, sid, tid, identity)
            if task.status in ACTIVE:
                if not body.visible or not valid_step(s, task, row):
                    task.status, task.error = "cancelled", "实验许可已撤销或过期。"
                else:
                    if task.status == "waiting":
                        task.status = "queued"
                    task.payload = {
                        **task.payload,
                        "permit": {**task.payload["permit"], "until": now().timestamp() + 15},
                    }
            return {"status": task.status}

    @router.post("/stories/{sid}/memory-snapshots")
    def snapshot_create(sid: str, body: SnapshotInput, identity=Depends(who)):
        from .role_memory import RoleMemoryService

        with db.session() as s:
            row = story(s, sid, identity)
            if not row.settings.get("paused"):
                raise HTTPException(409, "请先暂停剧情，再整理角色上下文。")
            person = actor(s, body.actor_id, identity)
            if person.story_id != sid or person.current_state.get("player"):
                raise HTTPException(404, "NPC不属于当前剧情。")
        try:
            task = RoleMemoryService(db, config, runtime, None).schedule(
                body.actor_id,
                identity,
                force=True,
                request_id=body.request_id,
                story_id=sid,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not task:
            return {
                "scheduled": False,
                "reason": "目前没有足够的新事件可整理；系统保留最近两条原始事件。",
            }
        return {"scheduled": True, "task_id": task.id}

    @router.get("/stories/{sid}/memory-snapshots/{tid}")
    def snapshot_get(sid: str, tid: str, identity=Depends(who)):
        with db.session() as s:
            story(s, sid, identity)
            task = s.get(Task, tid)
            person = s.get(Conversation, task.session_id) if task else None
            if (
                not task
                or task.kind != "role_reflect"
                or task.owner_id != identity
                or not person
                or person.story_id != sid
            ):
                raise HTTPException(404, "上下文整理任务不存在。")
            cut = task.result.get("cut")
            snapshot = (
                s.scalar(
                    select(RoleSnapshot)
                    .where(
                        RoleSnapshot.actor_id == person.id,
                        RoleSnapshot.covered_seq == cut,
                        RoleSnapshot.privacy_version == person.privacy_version,
                    )
                    .order_by(RoleSnapshot.created_at.desc())
                    .limit(1)
                )
                if cut is not None
                else None
            )
            return {
                "task_id": task.id,
                "actor_id": person.id,
                "status": task.status,
                "error": task.error,
                "cut": cut,
                "notes": task.result.get("notes"),
                "snapshot_id": snapshot.id if snapshot else None,
            }

    return router
