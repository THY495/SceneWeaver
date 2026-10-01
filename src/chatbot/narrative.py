"""Authored acyclic progression. Future nodes are never actor context."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from .directing import SceneDirection


class StageTransform(BaseModel):
    """Versioned visual placement in a 1600x900 frame, bottom-centre anchor."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: Literal[1] = 1
    x: float = Field(ge=-1600, le=3200)
    y: float = Field(ge=-900, le=1800)
    scale_x: float = Field(default=1, ge=0.1, le=4)
    scale_y: float = Field(default=1, ge=0.1, le=4)
    rotation: float = Field(default=0, ge=-180, le=180)


class StagePlacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = Field(min_length=1, max_length=80)
    position: Literal["left", "center", "right"] = "center"
    transform: StageTransform | None = None
    outfit: str | None = Field(default=None, min_length=1, max_length=80)
    variant: str | None = Field(default=None, min_length=1, max_length=80)
    draw_order: int | None = Field(default=None, ge=-1000, le=1000)
    editor_locked: bool = False
    editor_hidden: bool = False


class SceneObject(BaseModel):
    """Visual instance; item references never grant physical visibility."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["decoration", "item"]
    name: str = Field(min_length=1, max_length=100)
    item: str | None = Field(default=None, min_length=1, max_length=80)
    sprite: str = Field(pattern=r"^[a-f0-9]{64}$")
    transform: StageTransform
    layer: Literal["decoration", "entities", "foreground"] = "entities"
    order: int = Field(default=0, ge=-1000, le=1000)
    editor_locked: bool = False
    editor_hidden: bool = False

    @model_validator(mode="after")
    def item_reference(self):
        if (self.kind == "item") != bool(self.item):
            raise ValueError("真实物品必须绑定世界物品；装饰不能携带物品引用。")
        return self


class OpeningBlock(BaseModel):
    """Bounded authored prelude; speech is an utterance, never a confirmed fact."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=80)
    kind: Literal["narration", "dialogue"]
    text: str = Field(min_length=1, max_length=4000)
    speaker: str | None = Field(default=None, min_length=1, max_length=80)
    outfit: str | None = Field(default=None, min_length=1, max_length=80)
    variant: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def speaker_matches(self):
        if (self.kind == "dialogue") != bool(self.speaker):
            raise ValueError("固定对白必须指定人物，旁白不能指定说话者。")
        if self.kind != "dialogue" and (self.outfit or self.variant):
            raise ValueError("旁白不能指定人物造型或变体。")
        return self


class EditorPosition(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    x: float = Field(ge=-100000, le=100000)
    y: float = Field(ge=-100000, le=100000)


class VideoClip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: str = Field(min_length=1, max_length=80)
    allow_skip: bool = True


class EntryLocation(BaseModel):
    """Explicit world movement on entering a node; visual placement stays separate."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    actor: str = Field(min_length=1, max_length=80)
    location: str = Field(min_length=1, max_length=100)


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=160)
    editor_position: EditorPosition | None = None
    text: str = Field(default="", max_length=4000)
    observers: list[str] = Field(min_length=1, max_length=21)
    background: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    visual_mode: Literal["stage", "cg", "video"] = "stage"
    video: VideoClip | None = None
    stage: list[StagePlacement] | None = Field(default=None, max_length=20)
    objects: list[SceneObject] = Field(default_factory=list, max_length=100)
    mode: Literal["player", "backstage"] = "player"
    participants: list[str] = Field(default_factory=list, max_length=20)
    entry_locations: list[EntryLocation] = Field(default_factory=list, max_length=21)
    opening: list[OpeningBlock] | None = Field(default=None, min_length=1, max_length=16)
    direction: SceneDirection | None = None
    ending: bool = False

    @model_validator(mode="after")
    def unique_stage(self, info: ValidationInfo):
        if self.visual_mode == "video" and not self.video and not (info.context or {}).get("authoring_draft"):
            raise ValueError("视频场景需要选择视频素材。")
        if (
            self.visual_mode == "cg"
            and not self.background
            and not (info.context or {}).get("authoring_draft")
        ):
            raise ValueError("CG 场景需要选择主画面图片。")
        if (
            self.ending
            and (self.mode != "player" or self.direction)
            and not (info.context or {}).get("authoring_draft")
        ):
            raise ValueError("结束节点使用玩家场景和固定收尾；请将 AI 互动放在前置场景。")
        if len({o.id for o in self.objects}) != len(self.objects):
            raise ValueError("本场画面实例标识不能重复。")
        if not self.opening and not self.text and not (info.context or {}).get("authoring_draft"):
            raise ValueError("请填写开场积木或旧版开场内容。")
        if self.opening and len({b.id for b in self.opening}) != len(self.opening):
            raise ValueError("场内积木标识不能重复。")
        if self.stage and len({p.actor for p in self.stage}) != len(self.stage):
            raise ValueError("舞台人物不能重复。")
        if len({p.actor for p in self.entry_locations}) != len(self.entry_locations):
            raise ValueError("同一场景不能重复设置角色入场地点。")
        return self


class HoldingRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item: str = Field(min_length=1, max_length=80)
    actor: str = Field(min_length=1, max_length=80)


class Edge(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1, max_length=80)
    source: str
    target: str
    label: str = Field(min_length=1, max_length=160)
    trigger: Literal["manual", "ai"] = "manual"
    condition: str = Field(default="", max_length=1000)
    requires_holding: HoldingRequirement | None = None

    @model_validator(mode="after")
    def condition_required(self, info: ValidationInfo):
        if self.trigger == "ai" and not self.condition and not (info.context or {}).get("authoring_draft"):
            raise ValueError("AI 转场需要填写触发条件。")
        return self


class Narrative(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry: str
    nodes: list[Node] = Field(min_length=1, max_length=200)
    edges: list[Edge] = Field(default_factory=list, max_length=600)

    @model_validator(mode="after")
    def graph(self, info: ValidationInfo):
        ids = {n.id for n in self.nodes}
        if len(ids) != len(self.nodes) or self.entry not in ids:
            raise ValueError("剧情节点标识必须唯一，且入口必须存在。")
        if len({e.id for e in self.edges}) != len(self.edges):
            raise ValueError("剧情选项标识不能重复。")
        adjacency = {key: [] for key in ids}
        degree = dict.fromkeys(ids, 0)
        for edge in self.edges:
            if edge.source not in ids or edge.target not in ids:
                raise ValueError("剧情连线引用了不存在的节点。")
            if next(n for n in self.nodes if n.id == edge.source).ending and not (info.context or {}).get(
                "authoring_draft"
            ):
                raise ValueError("结束节点不能再连接后续场景。")
            adjacency[edge.source].append(edge.target)
            degree[edge.target] += 1
        queue = [key for key in ids if degree[key] == 0]
        visited = 0
        while queue:
            key = queue.pop()
            visited += 1
            for target in adjacency[key]:
                degree[target] -= 1
                if degree[target] == 0:
                    queue.append(target)
        if visited != len(ids):
            raise ValueError("剧情图不能包含循环；同一节点内可以持续对话。")
        reachable, pending = set(), [self.entry]
        while pending:
            key = pending.pop()
            if key not in reachable:
                reachable.add(key)
                pending.extend(adjacency[key])
        if reachable != ids and not (info.context or {}).get("authoring_draft"):
            raise ValueError("所有剧情节点必须能从入口到达。")
        return self


def enter_node(s, story, graph, node_id, actors, path):
    node = next(n for n in graph["nodes"] if n["id"] == node_id)
    if node.get("entry_locations"):
        from .observations import append_record, audience, members, notices

        by_id = {c.id: c for c in members(s, story.id)}
        for placement in node["entry_locations"]:
            actor = by_id[actors[placement["actor"]]]
            previous = actor.current_state.get("location", "main")
            destination = placement["location"]
            if previous == destination:
                continue
            before = {p.id for p in by_id.values() if p.id in audience(s, actor) and notices(p, actor)}
            actor.current_state = {**actor.current_state, "location": destination}
            actor.revision += 1
            after = {p.id for p in by_id.values() if p.id in audience(s, actor) and notices(p, actor)}
            append_record(
                s,
                story_id=story.id,
                kind="scene_change",
                actor=actor,
                text=f"{actor.current_state['profile']['name']}从{previous}来到{destination}。",
                observer_ids=sorted(before | after),
                dedupe=f"narrative-entry-location:{node_id}:{actor.id}",
                payload={"from": previous, "to": destination, "narrative_node": node_id},
            )
    participants = [actors[a] for a in node.get("participants", [])]
    if node.get("mode") == "backstage":
        from fastapi import HTTPException

        from .observations import members

        people = members(s, story.id)
        locations = {c.current_state.get("location", "main") for c in people if c.id in participants}
        if len(locations) != 1 or {
            c.id for c in people if c.current_state.get("location", "main") in locations
        } != set(participants):
            raise HTTPException(
                409, "幕后参与者必须实际同处一地，且该地点没有玩家或未列入的角色；请先在实验台调整位置。"
            )
    from .scene_execution import commit_block

    blocks = node.get("opening") or [{"id": "legacy", "kind": "narration", "text": node["text"]}]
    video_active = node.get("visual_mode") == "video" and node.get("mode", "player") == "player"
    controlled = (bool(node.get("opening")) or video_active) and node.get("mode", "player") == "player"
    if video_active:
        from .observations import append_record

        record = append_record(
            s,
            story_id=story.id,
            kind="host",
            text="片段开始。",
            observer_ids=[actors[a] for a in node["observers"]],
            channel="private",
            dedupe=f"narrative-video-start:{node_id}",
            payload={"source": "视频播放标记", "narrative_node": node_id},
        )
    else:
        for block in blocks[:1] if controlled else blocks:
            record = commit_block(s, story, node, block, actors)
    story.settings = {
        **story.settings,
        **({"paused": True} if node.get("mode") == "backstage" else {}),
        "narrative": {
            "node": node_id,
            "path": path,
            "event_id": record.id,
            "mode": node.get("mode", "player"),
            "participants": participants,
            **(
                {"execution": {"phase": "video" if video_active else "opening", "index": 0, "version": 1}}
                if controlled
                else {}
            ),
        },
    }
    if node.get("ending") and not controlled:
        from .scene_execution import finish_story

        finish_story(story)
    if node.get("mode") == "backstage" and "game_backstage_plan" in story.settings:
        from .storage import StoryEvent, uid

        plan = story.settings["game_backstage_plan"]
        hops = plan["hops"]
        hop = next((item for item in hops if item[0] == node_id), None)
        if not hop:
            raise ValueError("幕后节点不在独立游戏的固定开场路径中")
        if node_id in plan["directed"]:
            return  # The scoped game worker advances this scene after grounded beat review.
        edge = next(edge for edge in graph["edges"] if edge["id"] == hop[1])
        s.add(
            StoryEvent(
                id=uid(),
                story_id=story.id,
                request_id="game-scripted:" + node_id,
                payload={
                    "source": node_id,
                    "target": edge["target"],
                    "edge_id": edge["id"],
                    "kind": "fixed_backstage_prelude",
                },
            )
        )
        enter_node(s, story, graph, edge["target"], actors, [*path, edge["target"]])


def player_view(story, recipe, *, author=False):
    graph = recipe.get("narrative")
    if not graph:
        return {"enabled": False}
    state = story.settings["narrative"]
    node = next(n for n in graph["nodes"] if n["id"] == state["node"])
    if node.get("mode") == "backstage" and not author:
        game = "game_backstage_plan" in story.settings
        progress = story.settings.get("game_backstage_progress", {})
        status = (
            progress.get("status", "running")
            if progress.get("entry_event_id") == state.get("event_id")
            else "running"
        )
        return {
            "enabled": True,
            "mode": "backstage",
            "title": "幕后场景",
            "text": "其他角色正在推进剧情，请稍候。" if game else "此段由创作者在实验台推进。",
            **({"auto_status": status} if game else {}),
            "choices": [],
            "revision": len(state["path"]),
        }
    from .scene_execution import projection

    execution = projection(state, node)
    return {
        **({"execution": execution} if execution else {}),
        "ending": bool(node.get("ending")),
        "finished_at": state.get("finished_at"),
        "auto_ready": {"through_seq": story.settings["auto_route"]["through_seq"]}
        if story.settings.get("auto_route", {}).get("source_seq") == story.seq
        and story.settings["auto_route"].get("node") == node["id"]
        else None,
        "enabled": True,
        "mode": node.get("mode", "player"),
        "node_id": node["id"],
        "entry_event_id": state["event_id"],
        "title": node["title"],
        "text": ""
        if execution and execution["phase"] == "video"
        else (node.get("opening") or [{"text": node["text"]}])[0]["text"],
        "video": node.get("video") if execution and execution["phase"] == "video" else None,
        "revision": len(state["path"]),
        "background": node.get("background"),
        "visual_mode": node.get("visual_mode", "stage"),
        "choices": [
            {"id": e["id"], "label": e["label"]}
            for e in graph["edges"]
            if e["source"] == node["id"]
            and (author or e.get("trigger", "manual") == "manual")
            and (not execution or execution["phase"] == "interactive")
        ],
    }


class Choose(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=80)
    edge_id: str
    expected_revision: int = Field(ge=1)


class AutoContinue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    through_seq: int = Field(ge=1)


def install_routes(router, db, who):
    from fastapi import Depends, HTTPException
    from sqlalchemy import select

    from .experiment_initials import digest
    from .scene_execution import install_routes as install_execution
    from .storage import Conversation, ExperimentInitial, Story, StoryEvent, Task, uid

    install_execution(router, db, who)

    def owned(s, sid, identity):
        story = s.scalar(select(Story).where(Story.id == sid, Story.owner_id == identity).with_for_update())
        if not story:
            raise HTTPException(404, "故事不存在。")
        return story, s.get(ExperimentInitial, sid)

    @router.get("/stories/{sid}/narrative")
    def read(sid: str, identity=Depends(who)):
        with db.session.begin() as s:
            story, origin = owned(s, sid, identity)
            return player_view(story, origin.recipe) if origin else {"enabled": False}

    @router.get("/stories/{sid}/narrative/director")
    def director(sid: str, identity=Depends(who)):
        with db.session.begin() as s:
            story, origin = owned(s, sid, identity)
            return player_view(story, origin.recipe, author=True) if origin else {"enabled": False}

    @router.post("/stories/{sid}/narrative/choose")
    def choose(sid: str, body: Choose, identity=Depends(who)):
        return transition(sid, body, identity, False)

    @router.post("/stories/{sid}/narrative/director/choose")
    def director_choose(sid: str, body: Choose, identity=Depends(who)):
        return transition(sid, body, identity, True)

    @router.post("/stories/{sid}/narrative/auto-continue")
    def auto_continue(sid: str, body: AutoContinue, identity=Depends(who)):
        return transition(sid, body, identity, False, automatic=True)

    def transition(sid, body, identity, author, automatic=False):
        with db.session.begin() as s:
            story, origin = owned(s, sid, identity)
            if not origin:
                raise HTTPException(409, "此故事没有剧情图。")
            if automatic:
                proposal = story.settings.get("auto_route", {})
                if (
                    proposal.get("source_seq") != story.seq
                    or proposal.get("node") != story.settings.get("narrative", {}).get("node")
                    or body.through_seq != proposal.get("through_seq")
                ):
                    raise HTTPException(409, "转场依据已变化，请继续当前互动。")
                body = Choose(
                    request_id="auto:" + proposal["task_id"],
                    edge_id=proposal["edge_id"],
                    expected_revision=len(story.settings["narrative"]["path"]),
                )
            key = ("director:" if author else "narrative:") + digest(body.request_id)
            prior = s.scalar(
                select(StoryEvent).where(StoryEvent.story_id == sid, StoryEvent.request_id == key)
            )
            if prior:
                if prior.payload["request"] != body.model_dump():
                    raise HTTPException(409, "不能修改已经提交的同一选择请求。")
                return prior.payload["receipt"]
            graph = origin.recipe.get("narrative")
            if not graph:
                raise HTTPException(409, "此故事没有剧情图。")
            state = story.settings["narrative"]
            if state.get("mode") == "backstage" and not author:
                raise HTTPException(409, "幕后场景须由作者结束，玩家不能选择其中分支。")
            from .scene_execution import opening_active, story_finished

            if story_finished(story):
                raise HTTPException(409, "故事已结束，不能继续转场。")

            if opening_active(story):
                raise HTTPException(409, "请先完成当前开场，再选择场景出口。")
            if len(state["path"]) != body.expected_revision:
                raise HTTPException(409, "剧情已推进，请刷新当前节点。")
            if story.settings.get("paused") and not author:
                raise HTTPException(409, "请先开始或继续试玩。")
            edge = next(
                (e for e in graph["edges"] if e["id"] == body.edge_id and e["source"] == state["node"]), None
            )
            if not edge:
                raise HTTPException(409, "该选项不属于当前剧情节点。")
            from .auto_routing import holding_satisfied

            if not holding_satisfied(s, story, origin, edge):
                raise HTTPException(409, "物品持有条件尚未成立。")
            if edge.get("trigger") == "ai" and not automatic and not author:
                raise HTTPException(409, "该出口由互动条件触发，不能手动跳转。")
            people = list(
                s.scalars(select(Conversation).where(Conversation.story_id == sid).with_for_update())
            )
            ids = [c.id for c in people]
            if s.scalar(
                select(Task.id)
                .where(
                    Task.status.in_(["queued", "running", "waiting"]),
                    Task.kind.in_(["respond", "scene_run", "npc_step"]),
                    (Task.session_id.in_(ids)) | (Task.payload["story_id"].as_string() == sid),
                )
                .limit(1)
            ):
                raise HTTPException(409, "人物正在回应，请等待回应结束再选择剧情。")
            enter_node(
                s, story, graph, edge["target"], origin.receipt["actors"], [*state["path"], edge["target"]]
            )
            # Context snapshots prepared before the transition must be rebuilt.
            for actor in people:
                actor.revision += 1
            receipt = player_view(story, origin.recipe, author=author)
            s.add(
                StoryEvent(
                    id=uid(),
                    story_id=sid,
                    request_id=key,
                    payload={
                        "request": body.model_dump(),
                        "receipt": receipt,
                        "source": state["node"],
                        "target": edge["target"],
                        **({"auto_evidence": proposal} if automatic else {}),
                    },
                )
            )
            return receipt
