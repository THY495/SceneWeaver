import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chatbot.items.api import authoring_router
from chatbot.items.catalog import catalog, instantiate
from chatbot.items.models import ActorState, ItemDefinition, Limit, PhysicalState, Placement, World
from chatbot.items.rules import RuleViolation, location, preview_move, total_mass, validate_inventory


def place(kind="location", target="room"):
    return Placement(kind=kind, target=target)


def item(preset="paper-sheet", id="paper", kind="location", target="room"):
    return instantiate(preset, id, place(kind, target))


def world(*items, lift=10000, carry=10000):
    return World(
        items=list(items),
        actors=[
            ActorState(
                id="player",
                location="room",
                lift_limit_g=Limit(mode="limited", value=lift),
                carry_limit_g=Limit(mode="limited", value=carry),
            )
        ],
    )


def denied(code, w, id="paper", destination=None):
    before = w.model_dump()
    with pytest.raises(RuleViolation) as caught:
        preview_move(w, "player", id, 1, destination or place("actor", "player"))
    assert caught.value.code == code
    assert w.model_dump() == before


def test_catalog_and_copies():
    data = catalog()
    assert len(data["presets"]) == 15
    for p in data["presets"]:
        obj = item(p["id"])
        assert obj.state == obj.definition.initial_state
        assert obj.preset_version == 1
    a, b = item(), item()
    a.definition.tags.append("secret")
    assert "secret" not in b.definition.tags
    assert "secret" not in item().definition.tags
    seed = Path(__file__).parents[1] / "config/item-presets.v1.json"
    packaged = Path(__file__).parents[1] / "src/chatbot/items/presets.v1.json"
    assert json.loads(seed.read_text(encoding="utf-8")) == json.loads(packaged.read_text(encoding="utf-8"))


@pytest.mark.parametrize("value", [-1, True, 1.5, "5"])
def test_strict_quantities(value):
    with pytest.raises(ValidationError):
        Limit(mode="limited", value=value)
    with pytest.raises(ValidationError):
        ItemDefinition(id="x", name="x", mass_g=value)


def test_limit_modes_and_state():
    assert Limit(mode="limited", value=0).value == 0
    assert Limit().mode == "unset"
    assert Limit(mode="unlimited").value is None
    for value in [{"mode": "limited"}, {"mode": "unlimited", "value": 0}]:
        with pytest.raises(ValidationError):
            Limit(**value)
    with pytest.raises(ValidationError):
        PhysicalState(open=True, locked=True)


def test_category_capacity_and_no_mutation():
    envelope = item("envelope", "envelope")
    envelope.state.open = True
    paper = item()
    w = world(envelope, paper)
    result = preview_move(w, "player", "paper", 1, place("container", "envelope"))
    assert result.items[1].revision == 2
    assert w.items[1].placement.kind == "location"
    denied(
        "unsupported_category", world(envelope, item("metal-key")), destination=place("container", "envelope")
    )
    paper.definition.external_volume_ml = 21
    denied("capacity_exceeded", world(envelope, paper), destination=place("container", "envelope"))


def test_nested_mass_and_moving_contents():
    bag = item("backpack", "bag")
    box = item("wooden-box", "box", "container", "bag")
    paper = item(kind="container", target="box")
    w = world(bag, box, paper, carry=2605)
    validate_inventory(w)
    assert total_mass(w, bag) == 2605
    moved = preview_move(w, "player", "bag", 1, place("actor", "player"))
    assert moved.items[2].placement.target == "box"
    moved.actors[0].location = "elsewhere"
    assert location(moved, moved.items[2]) == "elsewhere"
    denied("carry_exceeded", world(bag, box, paper, carry=2604), id="bag")
    denied("lift_exceeded", world(bag, box, paper, lift=2604), id="bag")


@pytest.mark.parametrize("locked", [True, False])
def test_closed_ancestors(locked):
    bag = item("backpack", "bag")
    bag.state.locked = locked
    denied("container_inaccessible", world(bag, item(kind="container", target="bag")))
    denied("container_inaccessible", world(bag, item()), destination=place("container", "bag"))


def test_access_and_consent():
    denied("out_of_reach", world(item(target="elsewhere")))
    denied("not_movable", world(item("stone-statue")))
    denied("acceptance_required", world(item()), destination=place("actor", "npc"))
    w = world(item())
    w.actors.append(ActorState(id="npc", location="room"))
    w.items[0].placement = place("actor", "npc")
    denied("held_by_other", w)
    w.items[0].revision = 2
    denied("stale_revision", w)


def test_missing_quantity_and_toggle():
    paper = item()
    paper.definition.mass_g = None
    denied("missing_quantity", world(paper))
    w = world(paper)
    w.policy.check_mass = False
    assert preview_move(w, "player", "paper", 1, place("actor", "player"))
    w.policy.check_mass = True
    w.actors[0].lift_limit_g = Limit()
    denied("missing_limit", w)


def test_cycles_depth_and_destroyed_parent():
    a, b = item("cloth-pouch", "a"), item("cloth-pouch", "b", "container", "a")
    a.placement = place("container", "b")
    with pytest.raises(ValidationError, match="成环"):
        world(a, b)
    a.placement = place()
    a.state.destroyed = True
    with pytest.raises(ValidationError, match="销毁"):
        world(a, b)
    a.state.destroyed = False
    c = item(kind="container", target="b")
    with pytest.raises(ValidationError, match="深度"):
        World(items=[a, b, c], policy={"max_nesting": 1})


def test_rejection_precedence_and_contents_requirement():
    envelope = item("envelope", "envelope")
    envelope.state.open = True
    paper = item()
    paper.definition.tags.append("burning")
    denied("rejected_category", world(envelope, paper), destination=place("container", "envelope"))
    water = item("water-portion", "water", "container", "bag")
    bag = item("cloth-pouch", "bag")
    bag.definition.container.reject_any_tags = []
    with pytest.raises(RuleViolation) as caught:
        validate_inventory(world(bag, water))
    assert caught.value.code == "container_requirement"


def test_contents_count_and_ancestor_load():
    bag = item("backpack", "bag")
    bag.state.open = True
    bag.definition.container.max_items = Limit(mode="limited", value=0)
    denied("count_exceeded", world(bag, item()), destination=place("container", "bag"))
    bag.definition.container.max_items = Limit(mode="unlimited")
    bag.definition.container.max_contents_mass_g = Limit(mode="limited", value=2004)
    box = item("wooden-box", "box", "container", "bag")
    box.state.open = True
    denied("load_exceeded", world(bag, box, item()), destination=place("container", "box"))


def test_move_into_self_and_descendant_is_atomic():
    bag = item("backpack", "bag")
    bag.state.open = True
    box = item("wooden-box", "box", "container", "bag")
    box.state.open = True
    denied("invalid_containment", world(bag, box), id="bag", destination=place("container", "box"))
    denied("invalid_containment", world(bag), id="bag", destination=place("container", "bag"))


def test_owner_does_not_change_with_possession():
    paper = item()
    w = world(paper)
    w.actors.append(ActorState(id="npc", location="room"))
    paper.owner_id = "npc"
    result = preview_move(w, "player", "paper", 1, place("actor", "player"))
    assert result.items[0].owner_id == "npc"
    assert result.items[0].placement.target == "player"


def test_authoring_api_auth_and_preview():
    def owner(authorization: str | None = Header(default=None)):
        if authorization != "Bearer test":
            raise HTTPException(401)
        return "author"

    app = FastAPI()
    app.include_router(authoring_router(owner))
    with TestClient(app) as client:
        prefix = "/api/role/authoring/items"
        assert client.get(prefix + "/presets").status_code == 401
        client.headers["Authorization"] = "Bearer test"
        assert len(client.get(prefix + "/presets").json()["presets"]) == 15
        body = {
            "world": world(item()).model_dump(),
            "actor_id": "player",
            "item_id": "paper",
            "expected_revision": 1,
            "destination": place("actor", "player").model_dump(),
        }
        response = client.post(prefix + "/preview-move", json=body)
        assert response.status_code == 200
        assert response.json()["preview_only"] is True
        body["destination"] = place("actor", "npc").model_dump()
        assert client.post(prefix + "/preview-move", json=body).json()["code"] == "acceptance_required"
