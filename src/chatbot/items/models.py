"""Versioned authoring and runtime contracts; numbers never silently default to zero."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

Quantity = Annotated[int, Field(strict=True, ge=0)]
Key = Annotated[str, Field(min_length=1, max_length=80)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Limit(StrictModel):
    mode: Literal["unset", "unlimited", "limited"] = "unset"
    value: Quantity | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.mode == "limited") != (self.value is not None):
            raise ValueError("有限约束必须提供数值；未配置或不限不能携带数值。")
        return self


class Capabilities(StrictModel):
    movable: StrictBool = True
    can_contain: StrictBool = False
    can_be_contained: StrictBool = True


class Container(StrictModel):
    capacity_ml: Limit = Field(default_factory=Limit)
    max_contents_mass_g: Limit = Field(default_factory=Limit)
    max_items: Limit = Field(default_factory=Limit)
    accept_any_tags: list[Key] = Field(default_factory=list, max_length=30)
    reject_any_tags: list[Key] = Field(default_factory=list, max_length=30)


class PhysicalState(StrictModel):
    destroyed: StrictBool = False
    open: StrictBool = False
    locked: StrictBool = False
    opaque: StrictBool = True

    @model_validator(mode="after")
    def consistent(self):
        if self.open and self.locked:
            raise ValueError("容器不能同时处于打开与上锁状态。")
        return self


class ItemDefinition(StrictModel):
    id: Key
    name: str = Field(min_length=1, max_length=100)
    template_kind: Literal["prop", "container", "scenery"] = "prop"
    tags: list[Key] = Field(default_factory=list, max_length=30)
    mass_g: Quantity | None = None
    external_volume_ml: Quantity | None = None
    external_volume_mode: Literal["fixed"] = "fixed"
    capabilities: Capabilities = Field(default_factory=Capabilities)
    container: Container | None = None
    initial_state: PhysicalState = Field(default_factory=PhysicalState)
    requires_container_tags: list[Key] = Field(default_factory=list, max_length=30)
    notes: str = Field(default="", max_length=2000)
    # Rule execution is intentionally not implied by material tags.
    destruction_rule: None = None
    transformation_rules: list = Field(default_factory=list, max_length=0)

    @model_validator(mode="after")
    def coherent(self):
        if self.capabilities.can_contain != (self.container is not None):
            raise ValueError("容纳能力必须与容器配置同时设置。")
        if self.initial_state.open and self.initial_state.locked:
            raise ValueError("容器不能同时处于打开与上锁状态。")
        if self.container and self.container.capacity_ml.mode == "limited":
            if (
                self.external_volume_ml is not None
                and self.container.capacity_ml.value > self.external_volume_ml
            ):
                raise ValueError("固定外部体积不能小于内部容量。")
        return self


class Placement(StrictModel):
    kind: Literal["location", "actor", "container"]
    target: Key


class ItemInstance(StrictModel):
    id: Key
    definition: ItemDefinition
    placement: Placement
    state: PhysicalState = Field(default_factory=PhysicalState)
    owner_id: Key | None = None
    revision: Annotated[int, Field(strict=True, ge=1)] = 1
    source_instance_ids: list[Key] = Field(default_factory=list, max_length=30)
    preset_catalog: Key | None = None
    preset_version: Annotated[int, Field(strict=True, ge=1)] | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (self.preset_catalog is None) != (self.preset_version is None):
            raise ValueError("预设来源与版本必须同时提供。")
        if not self.definition.capabilities.can_contain and (self.state.open or self.state.locked):
            raise ValueError("非容器物品不能设置开关或锁状态。")
        return self


class TransferConsent(StrictModel):
    proposal_id: Key
    version: Annotated[int, Field(strict=True, ge=1)]
    item_id: Key


class ItemCommand(StrictModel):
    item_id: Key
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    action: Literal["move", "open", "close", "give"]
    destination: Placement | None = None
    agreement: TransferConsent | None = None

    @model_validator(mode="after")
    def coherent(self):
        if (self.action in {"move", "give"}) != (self.destination is not None):
            raise ValueError("移动须指定目标位置，开关容器不能附带移动。")
        if self.action == "give":
            if not self.agreement or self.destination.kind != "actor":
                raise ValueError("递交必须指定接收角色与明确同意的提案。")
        elif self.agreement:
            raise ValueError("只有递交可以引用交接提案。")
        return self


class ActorState(StrictModel):
    id: Key
    location: Key
    lift_limit_g: Limit = Field(default_factory=Limit)
    carry_limit_g: Limit = Field(default_factory=Limit)


class RulePolicy(StrictModel):
    check_volume: StrictBool = True
    check_mass: StrictBool = True
    max_nesting: Annotated[int, Field(strict=True, ge=1, le=20)] = 8


class World(StrictModel):
    items: list[ItemInstance] = Field(default_factory=list, max_length=200)
    actors: list[ActorState] = Field(default_factory=list, max_length=21)
    policy: RulePolicy = Field(default_factory=RulePolicy)

    @model_validator(mode="after")
    def references(self):
        items = {i.id: i for i in self.items}
        actors = {a.id for a in self.actors}
        if len(items) != len(self.items) or len(actors) != len(self.actors):
            raise ValueError("物品或角色标识重复。")
        for item in self.items:
            if not set(item.source_instance_ids) <= set(items):
                raise ValueError("物品来源引用了不存在的实例。")
            if item.owner_id is not None and item.owner_id not in actors:
                raise ValueError("物品所有人不存在。")
            seen = {item.id}
            current = item
            depth = 0
            while current.placement.kind == "container":
                parent = items.get(current.placement.target)
                if parent is None or not parent.definition.capabilities.can_contain:
                    raise ValueError("包含关系引用了不存在或不具备容纳能力的物品。")
                if parent.id in seen:
                    raise ValueError("容器包含关系不能成环。")
                seen.add(parent.id)
                depth += 1
                if depth > self.policy.max_nesting:
                    raise ValueError("容器嵌套超过当前允许深度。")
                if not current.state.destroyed and parent.state.destroyed:
                    raise ValueError("销毁容器前必须先明确处理其内容物。")
                current = parent
            if current.placement.kind == "actor" and current.placement.target not in actors:
                raise ValueError("物品持有人不存在。")
        return self
