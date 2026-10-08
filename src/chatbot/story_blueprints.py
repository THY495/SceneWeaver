"""Validated authoring inputs, separate from live stories and actor knowledge."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .items.models import World
from .items.rules import validate_inventory
from .narrative import Narrative
from .schemas import SessionProfile


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SpriteVariant(StrictInput):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=100)
    sprite: str = Field(pattern=r"^[a-f0-9]{64}$")
    expression: str | None = Field(default=None, max_length=100)
    pose: str | None = Field(default=None, max_length=100)


class Outfit(StrictInput):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=100)
    default_variant: str = Field(min_length=1, max_length=80)
    variants: list[SpriteVariant] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def valid_variants(self):
        ids = [v.id for v in self.variants]
        if len(set(ids)) != len(ids) or self.default_variant not in ids:
            raise ValueError("造型变体标识必须唯一，默认变体必须存在。")
        return self


class Presentation(StrictInput):
    version: Literal[1] = 1
    base_sprite: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    default_outfit: str | None = Field(default=None, min_length=1, max_length=80)
    outfits: list[Outfit] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def valid_outfits(self):
        ids = [o.id for o in self.outfits]
        if len(set(ids)) != len(ids) or (self.default_outfit and self.default_outfit not in ids):
            raise ValueError("人物造型标识必须唯一，默认造型必须存在。")
        return self

    def validate_selection(self, outfit=None, variant=None):
        selected = outfit or self.default_outfit or (self.outfits[0].id if self.outfits else None)
        group = next((o for o in self.outfits if o.id == selected), None)
        if (outfit and not group) or (
            variant and (not group or variant not in {v.id for v in group.variants})
        ):
            raise ValueError("舞台或开场引用了不存在的造型／变体。")
        return selected


class Person(StrictInput):
    alias: str = Field(min_length=1, max_length=80)
    profile: SessionProfile
    location: str = Field(min_length=1, max_length=100)
    goals: list[str] = Field(default_factory=list, max_length=20)
    unnoticed: list[str] = Field(default_factory=list, max_length=20)
    presentation: Presentation = Field(default_factory=Presentation)


class Player(StrictInput):
    alias: str = "player"
    name: str = Field(min_length=1, max_length=100)
    location: str = Field(min_length=1, max_length=100)


class Hearing(StrictInput):
    actor: str
    text: str = Field(min_length=1, max_length=4000)
    speaker_known: bool = True


class Speech(StrictInput):
    speaker: str
    text: str = Field(min_length=1, max_length=4000)
    heard: list[Hearing] = Field(default_factory=list, max_length=20)
    expects_reply: bool = False
    target: str | None = None


class Knowledge(StrictInput):
    text: str = Field(min_length=1, max_length=4000)
    kind: Literal["fact", "belief"] = "fact"
    observers: list[str] = Field(default_factory=list, max_length=21)


class ImageAsset(StrictInput):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["png"] = "png"
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class VideoAsset(StrictInput):
    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    kind: Literal["mp4"] = "mp4"
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(gt=0, le=256 * 1024 * 1024)


class Recipe(StrictInput):
    schema_version: Literal[1] = 1
    template_id: str = Field(default="custom", max_length=80)
    template_version: int = Field(default=1, ge=1)
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    world: str = Field(default="", max_length=2000)
    people: list[Person] = Field(min_length=1, max_length=20)
    player: Player
    speech: list[Speech] = Field(default_factory=list, max_length=40)
    knowledge: list[Knowledge] = Field(default_factory=list, max_length=80)
    narrative: Narrative | None = None
    item_world: World | None = None
    image_assets: list[ImageAsset] = Field(default_factory=list, max_length=256)
    video_assets: list[VideoAsset] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def references(self):
        if len({a.id for a in self.video_assets}) != len(self.video_assets) or len(
            {a.content_hash for a in self.video_assets}
        ) != len(self.video_assets):
            raise ValueError("视频素材标识和内容不能重复")
        if sum(a.bytes for a in self.video_assets) > 2 * 1024 * 1024 * 1024:
            raise ValueError("单个作品的视频总量不能超过 2 GB")
        if len({a.id for a in self.image_assets}) != len(self.image_assets) or len(
            {a.content_hash for a in self.image_assets}
        ) != len(self.image_assets):
            raise ValueError("图片素材标识和内容不能重复")
        aliases = [p.alias for p in self.people] + [self.player.alias]
        if self.item_world:
            locations = {p.alias: p.location for p in [*self.people, self.player]}
            if {a.id: a.location for a in self.item_world.actors} != locations:
                raise ValueError("物品配置的角色与地点必须和故事开局一致。")
            if any(i.revision != 1 for i in self.item_world.items):
                raise ValueError("初始物品修订号必须为1。")
            validate_inventory(self.item_world)
        if self.narrative:
            for edge in self.narrative.edges:
                requirement = edge.requires_holding
                if requirement and (
                    not self.item_world
                    or requirement.item not in {i.id for i in self.item_world.items}
                    or requirement.actor not in aliases
                ):
                    raise ValueError("转场物品条件必须引用已有物品与角色。")
                if any(actor not in {p.alias for p in self.people} for actor in edge.requires_agreement):
                    raise ValueError("转场同行同意只能引用工程中已有的 NPC。")
            for node in self.narrative.nodes:
                if any(p.actor not in aliases for p in node.entry_locations):
                    raise ValueError("场景入场地点只能引用工程中已有的人物或玩家。")
                if node.video and node.video.asset not in {asset.id for asset in self.video_assets}:
                    raise ValueError("视频场景必须引用已有视频素材。")
                if node.direction:
                    npc_aliases = {p.alias for p in self.people}
                    if any(not set(beat.participants) <= npc_aliases for beat in node.direction.beats):
                        raise ValueError("演出段落只能安排工程中已有的 NPC，不能替玩家表演。")
                people = {p.alias: p.presentation for p in self.people}
                selections = {}
                for placement in node.stage or []:
                    if placement.actor in people:
                        selections[placement.actor] = people[placement.actor].validate_selection(
                            placement.outfit, placement.variant
                        )
                for block in node.opening or []:
                    if block.speaker in people:
                        selections[block.speaker] = people[block.speaker].validate_selection(
                            block.outfit or selections.get(block.speaker), block.variant
                        )
                item_ids = {i.id for i in self.item_world.items} if self.item_world else set()
                if any(o.item and o.item not in item_ids for o in node.objects):
                    raise ValueError("画面物品必须引用工程中已有的世界物品。")
                for block in node.opening or []:
                    if block.speaker and block.speaker not in node.observers:
                        raise ValueError("固定对白的说话者必须是本场知情者，且引用已有角色。")
                    if block.target and block.target not in node.observers:
                        raise ValueError("固定对白的接收者必须是本场知情者，且引用已有角色。")
                if node.stage is not None and any(
                    p.actor not in {a.alias for a in self.people} for p in node.stage
                ):
                    raise ValueError("舞台配置只能引用已有 NPC，不能引用玩家或不存在的人物。")
                if (
                    not set(node.observers) <= set(aliases)
                    or len(set(node.observers)) != len(node.observers)
                    or (node.mode == "player" and self.player.alias not in node.observers)
                ):
                    raise ValueError("剧情节点知情者必须存在且不重复，并包含玩家；秘密请放入私有信息。")
                if node.mode == "backstage":
                    if (
                        not node.participants
                        or len(set(node.participants)) != len(node.participants)
                        or not set(node.participants) <= {p.alias for p in self.people}
                        or not set(node.observers) <= set(node.participants)
                    ):
                        raise ValueError("幕后场景须指定不重复的 NPC 参与者，知情者只能是其中的角色。")
        if len(set(aliases)) != len(aliases):
            raise ValueError("角色标识不能重复。")
        names = [p.profile.name for p in self.people] + [self.player.name]
        if len(set(names)) != len(names):
            raise ValueError("同一故事中的角色名不能重复。")
        for person in self.people:
            if not set(person.unnoticed) <= set(aliases) - {person.alias}:
                raise ValueError("未察觉对象必须是其他已存在角色。")
            if any(not g.strip() or len(g) > 2000 for g in person.goals):
                raise ValueError("目标需为1至2000字。")
            # Shared background is deliberately explicit; private facts belong in knowledge.
            person.profile.world = self.world
        for speech in self.speech:
            if speech.speaker not in aliases or any(h.actor not in aliases for h in speech.heard):
                raise ValueError("开场言论引用了不存在的角色。")
            heard = [h.actor for h in speech.heard]
            if len(set(heard)) != len(heard) or speech.speaker in heard:
                raise ValueError("听闻者不能重复或包含说话者。")
            if any(h.text not in speech.text for h in speech.heard):
                raise ValueError("听闻片段必须来自原话。")
            if speech.expects_reply != bool(speech.target):
                raise ValueError("明确回应必须同时指定回应角色。")
            if speech.target:
                hearing = next((h for h in speech.heard if h.actor == speech.target), None)
                if speech.target == speech.speaker or not hearing:
                    raise ValueError("回应角色必须是听到发言的其他角色。")
                if hearing.text != speech.text or not hearing.speaker_known:
                    raise ValueError("回应角色必须完整听到并辨认说话者。")
        for item in self.knowledge:
            if not set(item.observers) <= set(aliases) or len(set(item.observers)) != len(item.observers):
                raise ValueError("知情角色必须存在且不重复。")
        return self


class BlueprintSave(StrictInput):
    expected_version: int = Field(ge=0)
    recipe: Recipe


class JoinCharacter(StrictInput):
    request_id: str = Field(min_length=1, max_length=80)
    expected_seq: int = Field(ge=0)
    profile: SessionProfile
    location: str = Field(min_length=1, max_length=100)
    observer_ids: list[str] = Field(default_factory=list, max_length=20)
