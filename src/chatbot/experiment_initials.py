"""Atomic creation of isolated experiments from frozen initial recipes, never live clones."""

import hashlib
import json
from copy import deepcopy

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import select, text

from .characters import validate_profile
from .experiment_templates import TEMPLATES, template
from .observations import append_record
from .schemas import SessionProfile
from .storage import (
    Conversation,
    ExperimentInitial,
    RoleMemory,
    Story,
    StoryBlueprint,
    StoryEvent,
    Task,
    now,
    uid,
)
from .story_blueprints import BlueprintSave, JoinCharacter, Recipe


class ExperimentStart(BaseModel):
    request_id: str = Field(min_length=1, max_length=80)
    template_id: str | None = None
    source_story_id: str | None = None
    blueprint_id: str | None = None
    blueprint_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def one_source(self):
        if sum(bool(x) for x in (self.template_id, self.source_story_id, self.blueprint_id)) != 1:
            raise ValueError("请选择一个故事设定、模板或已有实验的初始记录。")
        if bool(self.blueprint_id) != (self.blueprint_version is not None):
            raise ValueError("故事设定必须指定版本。")
        return self


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def compatible_recipe_digests(recipe):
    """Accept the frozen input and its validated current-schema form when reopening old work."""
    return {digest(recipe), digest(Recipe.model_validate(recipe).model_dump())}


def model_settings(config):
    # Explicit allowlist: never export credentials, endpoint URLs or local key paths.
    return {
        key: getattr(config, key)
        for key in (
            "model_provider",
            "dialogue_model",
            "expert_model",
            "embedding_provider",
            "embedding_model",
            "context_soft_tokens",
            "context_hard_tokens",
            "daily_budget_usd",
            "turn_budget_usd",
        )
    }


def initial(s, sid, who):
    row = s.scalar(
        select(ExperimentInitial).where(ExperimentInitial.story_id == sid, ExperimentInitial.owner_id == who)
    )
    if not row:
        raise HTTPException(404, "该剧情没有可用的实验初始记录。")
    return row


def start(s, body, who, config, runtime, *, game_auto_backstage=False):
    # Serialize equal owner/request pairs before creating anything; the unique constraint is a backstop.
    lock = int.from_bytes(
        hashlib.sha256(f"experiment:{who}:{body.request_id}".encode()).digest()[:8], "big", signed=True
    )
    s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    request_hash = digest(body.model_dump())
    prior = s.scalar(
        select(ExperimentInitial).where(
            ExperimentInitial.owner_id == who, ExperimentInitial.request_id == body.request_id
        )
    )
    if prior:
        if prior.request_hash != request_hash:
            raise HTTPException(409, "同一请求标识不能更换实验起点。")
        return prior.receipt
    if body.blueprint_id:
        blueprint = s.scalar(
            select(StoryBlueprint)
            .where(StoryBlueprint.id == body.blueprint_id, StoryBlueprint.owner_id == who)
            .with_for_update()
        )
        if not blueprint:
            raise HTTPException(404, "故事设定不存在。")
        if blueprint.version != body.blueprint_version:
            raise HTTPException(409, "故事设定已更新，请重新读取后启动。")
        recipe = deepcopy(blueprint.recipe)
    elif body.source_story_id:
        recipe = deepcopy(initial(s, body.source_story_id, who).recipe)
    else:
        if body.template_id not in TEMPLATES:
            raise HTTPException(404, "实验模板不存在。")
        recipe = template(body.template_id)
    if recipe.get("schema_version") != 1:
        raise HTTPException(409, "当前程序不支持此初始记录版本。")
    # Disk projects may contain unfinished graphs. Validate the entire frozen
    # recipe before creating any story, observations or model tasks.
    try:
        recipe = Recipe.model_validate(recipe).model_dump()
    except ValidationError as exc:
        messages = "；".join(error["msg"].removeprefix("Value error, ") for error in exc.errors())
        raise HTTPException(422, "工程尚不能运行：" + messages) from None
    backstage_plan = {"hops": [], "directed": []}
    if game_auto_backstage:
        from .backstage_release import release_plan

        backstage_plan = release_plan(recipe.get("narrative"), recipe["people"], recipe["player"])
    settings = {
        "paused": True,
        "scene_enabled": True,
        "idle_enabled": True,
        "cooldown_seconds": 30,
        "chain_limit": 3,
        **({"game_backstage_plan": backstage_plan} if game_auto_backstage else {}),
    }
    story = Story(id=uid(), owner_id=who, title=recipe["title"], world=recipe["world"], settings=settings)
    s.add(story)
    s.flush()
    people = {}
    specs = [
        *recipe["people"],
        {
            **recipe["player"],
            "player": True,
            "goals": [],
            "unnoticed": [],
            "profile": {
                "name": recipe["player"]["name"],
                "core": "此角色由用户控制。",
                "world": recipe["world"],
                "relationships": [],
                "document_ids": [],
            },
        },
    ]
    for spec in specs:
        profile = SessionProfile.model_validate(spec["profile"])
        validate_profile(s, who, profile, config)
        c = Conversation(
            id=uid(),
            owner_id=who,
            story_id=story.id,
            title=profile.name + " · 实验",
            epoch=0,
            seq=0,
            revision=1,
            current_state={
                "profile": profile.model_dump(),
                "location": spec["location"],
                "player": bool(spec.get("player")),
                "scene_facts": {},
            },
        )
        s.add(c)
        people[spec["alias"]] = c
    s.flush()
    for spec in specs:
        c = people[spec["alias"]]
        c.current_state = {
            **c.current_state,
            "unnoticed_ids": [people[alias].id for alias in spec["unnoticed"]],
        }
        runtime.emit(
            s,
            c,
            "experiment_started",
            {"template_id": recipe["template_id"], "template_version": recipe["template_version"]},
        )
        for i, goal in enumerate(spec["goals"]):
            event = append_record(
                s,
                story_id=story.id,
                kind="authoring",
                text=goal,
                observer_ids=[c.id],
                channel="private",
                dedupe=f"initial-goal:{c.id}:{i}",
                payload={"kind": "goal"},
            )
            s.add(
                RoleMemory(
                    id=uid(),
                    story_id=story.id,
                    actor_id=c.id,
                    kind="goal",
                    content=goal,
                    source_ids=[event.id],
                    context_source_ids=[event.id],
                )
            )
    for i, item in enumerate(recipe.get("knowledge", [])):
        observers = [people[alias].id for alias in item["observers"]]
        record = append_record(
            s,
            story_id=story.id,
            kind="host",
            text=item["text"],
            observer_ids=observers,
            channel="private",
            dedupe=f"initial-knowledge:{i}",
            payload={
                "key": f"initial-knowledge-{i}",
                "value": item["text"],
                "event_id": uid(),
                "basis": "confirmed" if item["kind"] == "fact" else "hearsay",
                "source": "初始设定",
                "occurred_at": "开场前",
                "learned_at": "开场前",
            },
        )
        for actor_id in observers:
            s.add(
                RoleMemory(
                    id=uid(),
                    story_id=story.id,
                    actor_id=actor_id,
                    kind="belief",
                    content=item["text"],
                    source_ids=[record.id],
                    context_source_ids=[record.id],
                )
            )
    for i, speech in enumerate(recipe["speech"]):
        speaker = people[speech["speaker"]]
        perceptions = {
            people[h["actor"]].id: {"text": h["text"], "speaker_known": h["speaker_known"]}
            for h in speech["heard"]
        }
        append_record(
            s,
            story_id=story.id,
            kind="speech",
            text=speech["text"],
            actor=speaker,
            observer_ids=[speaker.id, *perceptions],
            dedupe=f"initial-speech:{i}",
            perceptions=perceptions,
            payload={
                "exchange": {
                    "expects_reply": True,
                    "target_id": people[speech["target"]].id,
                }
            }
            if speech.get("expects_reply")
            else {},
        )
    receipt = {
        "story_id": story.id,
        "player_id": people[recipe["player"]["alias"]].id,
        "actors": {alias: c.id for alias, c in people.items()},
        "initial_seq": story.seq,
        "recipe_hash": digest(recipe),
        "settings": settings,
        "model_settings": model_settings(config),
        "source_story_id": body.source_story_id,
        "blueprint_id": body.blueprint_id,
        "blueprint_version": body.blueprint_version,
    }
    origin = ExperimentInitial(
        story_id=story.id,
        owner_id=who,
        request_id=body.request_id,
        request_hash=request_hash,
        recipe=recipe,
        receipt=receipt,
    )
    s.add(origin)
    s.flush()
    # Visual event snapshots need the frozen recipe before the first opening line.
    if recipe.get("narrative"):
        from .narrative import enter_node

        graph = recipe["narrative"]
        enter_node(
            s, story, graph, graph["entry"], {alias: c.id for alias, c in people.items()}, [graph["entry"]]
        )
    if recipe.get("item_world"):
        from .items.persistence import initialize

        initialize(s, story, recipe["item_world"], people)
    if game_auto_backstage and story.settings.get("narrative", {}).get("mode") != "backstage":
        # "New game" is the player's start action. Only an unfinished backstage
        # entry stays paused until its autonomous introduction reaches the player.
        story.settings = {**story.settings, "paused": False}
    receipt = {**receipt, "initial_seq": story.seq}
    origin.receipt = receipt
    return receipt


def experiment_router(db, config, runtime, owner):
    router = APIRouter(prefix="/api/role/experiments")

    def who(identity=Depends(owner)):
        if config.product != "roleplay":
            raise HTTPException(404)
        return identity

    @router.get("/templates")
    def templates(identity=Depends(who)):
        return [
            {key: value[key] for key in ("template_id", "template_version", "title", "description")}
            for value in TEMPLATES.values()
        ]

    @router.post("/start", status_code=201)
    async def create(body: ExperimentStart, identity=Depends(who)):
        await runtime.model_readiness.require()
        with db.session.begin() as s:
            return start(s, body, identity, config, runtime)

    @router.get("/templates/{template_id}")
    def read_template(template_id: str, identity=Depends(who)):
        if template_id not in TEMPLATES:
            raise HTTPException(404, "模板不存在。")
        return template(template_id)

    @router.get("/blueprints")
    def blueprints(identity=Depends(who)):
        with db.session() as s:
            return [
                dict(id=row.id, version=row.version, recipe=row.recipe, updated_at=row.updated_at)
                for row in s.scalars(
                    select(StoryBlueprint)
                    .where(StoryBlueprint.owner_id == identity)
                    .order_by(StoryBlueprint.updated_at.desc())
                )
            ]

    @router.post("/validate")
    def validate_blueprint(body: BlueprintSave, identity=Depends(who)):
        with db.session() as s:
            for person in body.recipe.people:
                validate_profile(s, identity, person.profile, config)
        return body.recipe.model_dump()

    @router.post("/stories/{sid}/characters", status_code=201)
    def join_character(sid: str, body: JoinCharacter, identity=Depends(who)):
        with db.session.begin() as s:
            story = s.scalar(
                select(Story).where(Story.id == sid, Story.owner_id == identity).with_for_update()
            )
            if not story:
                raise HTTPException(404, "剧情不存在。")
            key = "join:" + digest(body.request_id)
            prior = s.scalar(
                select(StoryEvent).where(StoryEvent.story_id == sid, StoryEvent.request_id == key)
            )
            if prior:
                if prior.payload["request"] != body.model_dump():
                    raise HTTPException(409, "不能以同一请求修改加入角色的内容。")
                return prior.payload["receipt"]
            if not story.settings.get("paused") or story.seq != body.expected_seq:
                raise HTTPException(409, "请暂停并刷新剧情后再加入角色。")
            people = list(
                s.scalars(select(Conversation).where(Conversation.story_id == sid).with_for_update())
            )
            ids = [c.id for c in people]
            if s.scalar(
                select(Task.id)
                .where(
                    Task.status.in_(["queued", "running", "waiting"]),
                    (Task.session_id.in_(ids)) | (Task.payload["story_id"].as_string() == sid),
                )
                .limit(1)
            ):
                raise HTTPException(409, "请等待本次运行任务结束后再加入角色。")
            if len(people) >= 21 or any(
                c.current_state.get("profile", {}).get("name") == body.profile.name for c in people
            ):
                raise HTTPException(409, "角色数量达到上限或名称已存在。")
            if not set(body.observer_ids) <= {
                c.id for c in people if c.current_state.get("location") == body.location
            }:
                raise HTTPException(422, "察觉到场的角色必须位于相同地点。")
            validate_profile(s, identity, body.profile, config)
            actor = Conversation(
                id=uid(),
                owner_id=identity,
                story_id=sid,
                title=body.profile.name + " · 实验",
                epoch=0,
                seq=0,
                revision=1,
                current_state={
                    "profile": body.profile.model_dump(),
                    "location": body.location,
                    "player": False,
                    "scene_facts": {},
                },
            )
            s.add(actor)
            s.flush()
            for c in people:
                c.current_state = (
                    {
                        **c.current_state,
                        "unnoticed_ids": [*c.current_state.get("unnoticed_ids", []), actor.id],
                    }
                    if c.id not in body.observer_ids
                    else dict(c.current_state)
                )
                c.revision += 1
            event = append_record(
                s,
                story_id=sid,
                kind="arrival",
                actor=actor,
                text=f"{body.profile.name}来到{body.location}。",
                observer_ids=[actor.id, *body.observer_ids],
                dedupe=key,
            )
            receipt = {"actor_id": actor.id, "event_id": event.id}
            s.add(
                StoryEvent(
                    id=uid(),
                    story_id=sid,
                    request_id=key,
                    payload={"request": body.model_dump(), "receipt": receipt},
                )
            )
            return receipt

    @router.put("/blueprints/{bid}")
    def save_blueprint(bid: str, body: BlueprintSave, identity=Depends(who)):
        from uuid import UUID

        from .project_workspace import project_root

        try:
            UUID(bid)
        except ValueError:
            raise HTTPException(422, "故事标识格式无效。") from None
        with db.session.begin() as s:
            lock = int.from_bytes(
                hashlib.sha256(f"blueprint:{bid}".encode()).digest()[:8], "big", signed=True
            )
            s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
            if (project_root(config, identity) / str(UUID(bid))).exists():
                raise HTTPException(409, "磁盘工程请从项目管理器保存，旧设定接口不能改写工程")
            row = s.get(StoryBlueprint, bid)
            if row and row.owner_id != identity:
                raise HTTPException(404, "故事设定不存在。")
            recipe = body.recipe.model_dump()
            for person in body.recipe.people:
                validate_profile(s, identity, person.profile, config)
            if row and row.recipe == recipe and row.version == body.expected_version + 1:
                return dict(id=row.id, version=row.version, recipe=row.recipe)
            if (row.version if row else 0) != body.expected_version:
                raise HTTPException(409, "故事设定已被修改，请返回故事库重新打开；当前草稿仍保留在编辑器。")
            if row:
                row.recipe, row.version, row.updated_at = recipe, row.version + 1, now()
            else:
                row = StoryBlueprint(id=bid, owner_id=identity, version=1, recipe=recipe)
                s.add(row)
            return dict(id=row.id, version=row.version, recipe=row.recipe)

    @router.get("/stories/{sid}/initial")
    def export(sid: str, identity=Depends(who)):
        with db.session() as s:
            row = initial(s, sid, identity)
            return {
                "format": "sceneweaver-experiment-initial",
                "format_version": 1,
                "created_at": row.created_at,
                "recipe": row.recipe,
                "receipt": row.receipt,
                "limitations": [
                    "初始条件记录，不包含后续轨迹或当前状态快照。",
                    "模型配置仅作记录；重新创建使用当前服务配置，不保证逐字复现。",
                ],
            }

    from .narrative import install_routes

    install_routes(router, db, who)
    return router
