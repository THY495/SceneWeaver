import pytest

from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Limit, Placement, World
from chatbot.items.perception import visible_items
from chatbot.items.rules import RuleViolation, preview_container_state, preview_move


def setup():
    actor = ActorState(
        id="player",
        location="room",
        lift_limit_g=Limit(mode="unlimited"),
        carry_limit_g=Limit(mode="unlimited"),
    )
    bag = instantiate("backpack", "bag", Placement(kind="location", target="room"))
    box = instantiate("wooden-box", "box", Placement(kind="container", target="bag"))
    box.state.locked = True
    letter = instantiate("paper-sheet", "letter", Placement(kind="container", target="box"))
    return World(items=[bag, box, letter], actors=[actor])


def names(world, actor="player", noticed=None):
    return {i["id"] for i in visible_items(world, actor, noticed or set())}


def test_carry_closed_nested_container_does_not_reveal_contents():
    world = setup()
    assert names(world) == {"bag"}
    carried = preview_move(world, "player", "bag", 1, Placement(kind="actor", target="player"))
    assert names(carried) == {"bag"}
    opened = preview_container_state(carried, "player", "bag", 2, opened=True)
    assert names(opened) == {"bag", "box"}
    assert names(carried) == {"bag"}
    with pytest.raises(RuleViolation) as error:
        preview_container_state(opened, "player", "box", 1, opened=True)
    assert error.value.code == "locked"
    with pytest.raises(RuleViolation) as error:
        preview_move(opened, "player", "letter", 1, Placement(kind="actor", target="player"))
    assert error.value.code == "container_inaccessible"
    assert "letter" not in str(visible_items(opened, "player", set()))


def test_transparent_container_visible_but_not_reachable():
    world = setup()
    world.items[0].state.opaque = False
    assert names(world) == {"bag", "box"}
    with pytest.raises(RuleViolation) as error:
        preview_move(world, "player", "box", 1, Placement(kind="actor", target="player"))
    assert error.value.code == "container_inaccessible"


def test_unnoticed_holder_and_other_location_do_not_leak_contents():
    world = setup()
    world.actors.append(ActorState(id="npc", location="room"))
    world.items[0].placement = Placement(kind="actor", target="npc")
    world.items[0].state.open = True
    assert names(world) == set()
    assert names(world, noticed={"npc"}) == {"bag", "box"}
    world.actors[1].location = "other"
    assert names(world, noticed={"npc"}) == set()


def test_open_close_revision_and_nested_access():
    world = setup()
    before = world.model_dump()
    with pytest.raises(RuleViolation) as error:
        preview_container_state(world, "player", "box", 1, opened=True)
    assert error.value.code == "container_inaccessible"
    assert world.model_dump() == before
    opened = preview_container_state(world, "player", "bag", 1, opened=True)
    with pytest.raises(RuleViolation) as error:
        preview_container_state(opened, "player", "bag", 1, opened=False)
    assert error.value.code == "stale_revision"
    closed = preview_container_state(opened, "player", "bag", 2, opened=False)
    assert closed.items[0].revision == 3
    assert names(closed) == {"bag"}
