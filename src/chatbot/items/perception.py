"""Current physical visibility, not remembered knowledge or permission to take an item."""

from .models import World
from .rules import location, root_placement


def visible_items(world: World, actor_id: str, noticed_actor_ids: set[str]):
    actor = next((a for a in world.actors if a.id == actor_id), None)
    if actor is None:
        raise ValueError("观察角色不存在。")
    index = {i.id: i for i in world.items}
    visible = []
    for item in world.items:
        if item.state.destroyed or location(world, item) != actor.location:
            continue
        root = root_placement(world, item)
        if root.kind == "actor" and root.target not in {actor_id, *noticed_actor_ids}:
            continue
        current = item
        obscured = False
        while current.placement.kind == "container":
            current = index[current.placement.target]
            if current.state.opaque and not current.state.open:
                obscured = True
                break
        if obscured:
            continue
        # No definitions, material rules, ownership, contents, or text to read here.
        visible.append(
            {
                "id": item.id,
                "name": item.definition.name,
                "version": item.revision,
                "location": actor.location,
                "holder_id": root.target if root.kind == "actor" else None,
                "container_id": item.placement.target if item.placement.kind == "container" else None,
                "is_container": item.definition.capabilities.can_contain,
                "open": item.state.open if item.definition.capabilities.can_contain else None,
            }
        )
    return visible
