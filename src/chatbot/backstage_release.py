"""The deliberately small, deterministic backstage subset a game can publish."""


def release_plan(graph, people, player):
    """Bound the automatic entry chain before the first player-facing scene.

    A backstage scene reached after player interaction still needs an autonomous
    model scheduler and a location policy. Neither can be inferred from a DAG.
    """
    if not graph:
        return {"hops": [], "directed": []}
    nodes = {node["id"]: node for node in graph["nodes"]}
    backstage = {node_id for node_id, node in nodes.items() if node.get("mode") == "backstage"}
    if not backstage:
        return {"hops": [], "directed": []}
    locations = {person["alias"]: person["location"] for person in people}
    locations[player["alias"]] = player["location"]
    hops = []
    directed = []
    current = graph["entry"]
    seen = set()
    while current in backstage:
        if current in seen:
            raise ValueError("幕后场景不能形成循环")
        seen.add(current)
        node = nodes[current]
        for placement in node.get("entry_locations", []):
            locations[placement["actor"]] = placement["location"]
        exits = [edge for edge in graph["edges"] if edge["source"] == current]
        participants = set(node["participants"])
        places = {locations[alias] for alias in participants}
        colocated = {alias for alias, place in locations.items() if place in places}
        direction = node.get("direction")
        if (
            node.get("visual_mode", "stage") != "stage"
            or len(exits) != 1
            or exits[0].get("trigger", "manual") != "manual"
            or exits[0].get("requires_holding")
            or len(places) != 1
            or colocated != participants
        ):
            raise ValueError(
                f"幕后场景「{node['title']}」尚不能在独立游戏中自动运行；"
                "当前仅支持入口处同场 NPC 和唯一无条件出口"
            )
        if direction:
            beats = direction.get("beats") or []
            if (
                not beats
                or len(beats) > 8
                or any(not set(beat["participants"]) <= participants for beat in beats)
            ):
                raise ValueError("独立游戏幕后 AI 场景最多允许八个段落，参与者须在本场 NPC 中")
            directed.append(current)
        edge = exits[0]
        hops.append((current, edge["id"], edge["target"]))
        current = edge["target"]
    if seen != backstage:
        raise ValueError("独立游戏仅支持从入口连续经过的固定幕后场景；互动后的幕后场景尚需自动调度")
    return {"hops": hops, "directed": directed}


def scripted_prefix(graph, people, player):
    """Legacy fixed-only selector retained for existing authoring checks."""
    plan = release_plan(graph, people, player)
    if plan["directed"]:
        raise ValueError("带导演编排的幕后场景不属于固定对白开场")
    return plan["hops"]
