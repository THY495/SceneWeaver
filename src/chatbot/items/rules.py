"""Pure preflight. Caller supplies authoritative state and separately authorizes perception.

No database, LLM, mutation or claim of actual execution. In particular, a successful
recipient check cannot stand in for acceptance of an offered item.
"""

from .models import ItemInstance, Placement, World


class RuleViolation(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(code, message):
    raise RuleViolation(code, message)


def children(world, parent):
    return [
        i
        for i in world.items
        if not i.state.destroyed and i.placement.kind == "container" and i.placement.target == parent
    ]


def total_mass(world: World, item: ItemInstance):
    values = [item.definition.mass_g, *(total_mass(world, c) for c in children(world, item.id))]
    return None if None in values else sum(values)


def root_placement(world, item):
    index = {i.id: i for i in world.items}
    while item.placement.kind == "container":
        item = index[item.placement.target]
    return item.placement


def location(world, item):
    root = root_placement(world, item)
    if root.kind == "actor":
        return next(a.location for a in world.actors if a.id == root.target)
    return root.target


def check_limit(limit, amount, code):
    if limit.mode == "unset":
        fail("missing_limit", "已启用的约束尚未填写限制。")
    if limit.mode == "unlimited":
        return
    if amount is None:
        fail("missing_quantity", "缺少本次判断必需的重量或体积。")
    if amount > limit.value:
        fail(
            code,
            {
                "capacity_exceeded": "容器剩余容量不足。",
                "load_exceeded": "容器内容物超过承重。",
                "count_exceeded": "容器物品数量超过上限。",
                "lift_exceeded": "角色搬不动该物品。",
                "carry_exceeded": "角色携带后将超过总负重。",
            }[code],
        )


def quantity_sum(values):
    values = list(values)
    return None if None in values else sum(values)


def accessible(world, actor, item):
    if item.state.destroyed:
        fail("destroyed", "物品已销毁。")
    if location(world, item) != actor.location:
        fail("out_of_reach", "物品不在可接触地点。")
    root = root_placement(world, item)
    if root.kind == "actor" and root.target != actor.id:
        fail("held_by_other", "物品由其他角色控制，需要相应交互许可。")
    index = {i.id: i for i in world.items}
    current = item
    while current.placement.kind == "container":
        current = index[current.placement.target]
        if current.state.locked or not current.state.open:
            fail("container_inaccessible", "物品所在容器未打开或已上锁。")


def _validate_inventory(world):
    for parent in world.items:
        contents = children(world, parent.id)
        if not contents:
            continue
        config = parent.definition.container
        for item in contents:
            definition = item.definition
            if not definition.capabilities.can_be_contained:
                fail("not_containable", "物品不允许放入容器。")
            tags = set(definition.tags)
            if tags & set(config.reject_any_tags):
                fail("rejected_category", "容器不接受此类物品。")
            if config.accept_any_tags and not tags & set(config.accept_any_tags):
                fail("unsupported_category", "物品类别不在容器接纳范围内。")
            if not set(definition.requires_container_tags) <= set(parent.definition.tags):
                fail("container_requirement", "容器不满足物品的存放要求。")
        check_limit(config.max_items, len(contents), "count_exceeded")
        if world.policy.check_volume:
            check_limit(
                config.capacity_ml,
                quantity_sum(c.definition.external_volume_ml for c in contents),
                "capacity_exceeded",
            )
        if world.policy.check_mass:
            check_limit(
                config.max_contents_mass_g,
                quantity_sum(total_mass(world, c) for c in contents),
                "load_exceeded",
            )
    if world.policy.check_mass:
        for actor in world.actors:
            roots = [
                i
                for i in world.items
                if not i.state.destroyed and i.placement.kind == "actor" and i.placement.target == actor.id
            ]
            if roots:
                check_limit(
                    actor.carry_limit_g, quantity_sum(total_mass(world, i) for i in roots), "carry_exceeded"
                )


def validate_inventory(world):
    """Check initial placements, including closed containers, without opening anything."""
    _validate_inventory(world)


def preview_container_state(
    world: World, actor_id: str, item_id: str, expected_revision: int, *, opened: bool
) -> World:
    """Opening is an explicit action, never a side effect of looking in the inventory."""
    actor = next((a for a in world.actors if a.id == actor_id), None)
    item = next((i for i in world.items if i.id == item_id), None)
    if actor is None or item is None:
        fail("unknown_reference", "角色或物品不存在。")
    if item.revision != expected_revision:
        fail("stale_revision", "物品状态已变化。")
    accessible(world, actor, item)
    if not item.definition.capabilities.can_contain:
        fail("not_container", "目标不是有效容器。")
    if item.state.locked:
        fail("locked", "容器已上锁，需要先满足解锁条件。")
    if item.state.open == opened:
        fail("no_change", "容器已经处于该状态。")
    data = world.model_dump()
    changed = next(i for i in data["items"] if i["id"] == item_id)
    changed["state"]["open"] = opened
    changed["revision"] += 1
    return World.model_validate(data)


def preview_move(
    world: World, actor_id: str, item_id: str, expected_revision: int, destination: Placement
) -> World:
    actor = next((a for a in world.actors if a.id == actor_id), None)
    item = next((i for i in world.items if i.id == item_id), None)
    if actor is None or item is None:
        fail("unknown_reference", "角色或物品不存在。")
    if item.revision != expected_revision:
        fail("stale_revision", "物品状态已变化。")
    accessible(world, actor, item)
    if not item.definition.capabilities.movable:
        fail("not_movable", "物品不允许直接搬动。")
    if destination == item.placement:
        fail("no_change", "物品已经位于目标位置。")
    if world.policy.check_mass:
        check_limit(actor.lift_limit_g, total_mass(world, item), "lift_exceeded")
    if destination.kind == "actor" and destination.target != actor.id:
        fail("acceptance_required", "递交需要单独的接受流程，不能直接更换持有人。")
    if destination.kind == "location" and destination.target != actor.location:
        fail("out_of_reach", "只能放置到当前可接触地点。")
    if destination.kind == "container":
        parent = next((i for i in world.items if i.id == destination.target), None)
        if parent is None or not parent.definition.capabilities.can_contain:
            fail("not_container", "目标不是有效容器。")
        accessible(world, actor, parent)
        if parent.state.locked or not parent.state.open:
            fail("container_inaccessible", "目标容器未打开或已上锁。")
    data = world.model_dump()
    changed = next(i for i in data["items"] if i["id"] == item_id)
    changed["placement"] = destination.model_dump()
    changed["revision"] += 1
    try:
        candidate = World.model_validate(data)
    except ValueError:
        fail("invalid_containment", "移动将产生无效包含关系或超过嵌套深度。")
    _validate_inventory(candidate)
    return candidate


def preview_transfer(world, sender_id, recipient_id, item_id, expected_revision):
    """Physical validation only; execution must separately authorize exact consent."""
    sender = next((a for a in world.actors if a.id == sender_id), None)
    recipient = next((a for a in world.actors if a.id == recipient_id), None)
    item = next((i for i in world.items if i.id == item_id), None)
    if not sender or not recipient or not item or sender_id == recipient_id:
        fail("unknown_reference", "交接对象不可用。")
    if sender.location != recipient.location:
        fail("out_of_reach", "接收者不在同一地点。")
    if root_placement(world, item) != Placement(kind="actor", target=sender_id):
        fail("not_holder", "只能递交自己实际持有的物品。")
    # Reuse accessibility, destroyed/movable, version and sender lifting checks.
    held = preview_move(
        world, sender_id, item_id, expected_revision, Placement(kind="location", target=sender.location)
    )
    moved = next(i for i in held.items if i.id == item_id)
    moved.placement = Placement(kind="actor", target=recipient_id)
    if world.policy.check_mass:
        check_limit(recipient.lift_limit_g, total_mass(world, item), "lift_exceeded")
    _validate_inventory(held)
    return held
