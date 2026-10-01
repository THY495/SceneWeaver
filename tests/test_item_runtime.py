from uuid import uuid4

from fastapi.testclient import TestClient

from chatbot.api import create_app
from chatbot.experiment_templates import template
from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Limit, Placement, World
from chatbot.storage import ItemWorldState


def recipe():
    value = template("partial-hearing")
    bag = instantiate("backpack", "bag", Placement(kind="location", target=value["player"]["location"]))
    box = instantiate("wooden-box", "box", Placement(kind="container", target="bag"))
    box.state.locked = True
    letter = instantiate("paper-sheet", "letter", Placement(kind="container", target="box"))
    actors = [
        ActorState(
            id=p["alias"],
            location=p["location"],
            lift_limit_g=Limit(mode="limited", value=10000),
            carry_limit_g=Limit(mode="limited", value=10000),
        )
        for p in [*value["people"], value["player"]]
    ]
    value["item_world"] = World(items=[bag, box, letter], actors=actors).model_dump()
    return value


def start(client, request="one", blueprint=None):
    base = "/api/role/experiments"
    bid = blueprint or str(uuid4())
    if blueprint is None:
        response = client.put(base + "/blueprints/" + bid, json={"expected_version": 0, "recipe": recipe()})
        assert response.status_code == 200, response.text
    response = client.post(
        base + "/start", json={"request_id": request, "blueprint_id": bid, "blueprint_version": 1}
    )
    assert response.status_code == 201, response.text
    return bid, response.json()


def test_initialization_isolation_moves_and_privacy(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        bid, run = start(client)
        _, same = start(client, blueprint=bid)
        assert same == run
        _, other = start(client, "two", bid)
        path = f"/api/role/items/actors/{run['player_id']}"
        view = client.get(path).json()["items"]
        assert len(view) == 1 and view[0]["name"] == "日常背包"
        assert "小木箱" not in str(view) and "普通信纸" not in str(view)
        body = {
            "request_id": "take",
            "item_id": view[0]["id"],
            "expected_revision": 1,
            "action": "move",
            "destination": {"kind": "actor", "target": run["player_id"]},
        }
        moved = client.post(path + "/actions", json=body)
        assert moved.status_code == 200, moved.text
        assert moved.json()["items"][0]["holder_id"] == run["player_id"]
        assert client.post(path + "/actions", json=body).json() == moved.json()
        assert client.post(path + "/actions", json={**body, "request_id": "stale"}).status_code == 409
        opened = client.post(
            path + "/actions",
            json={"request_id": "open", "item_id": view[0]["id"], "expected_revision": 2, "action": "open"},
        )
        assert opened.status_code == 200, opened.text
        box = next(i for i in opened.json()["items"] if i["name"] == "小木箱")
        assert "普通信纸" not in str(opened.json())
        locked = client.post(
            path + "/actions",
            json={"request_id": "box", "item_id": box["id"], "expected_revision": 1, "action": "open"},
        )
        assert locked.status_code == 409 and locked.json()["detail"]["code"] == "locked"
        other_view = client.get(f"/api/role/items/actors/{other['player_id']}").json()["items"]
        assert len(other_view) == 1 and other_view[0]["id"] != view[0]["id"]
        assert other_view[0]["version"] == 1 and other_view[0]["holder_id"] is None
        with db.session() as s:
            state = s.get(ItemWorldState, run["story_id"])
            assert len(state.state["items"]) == 3 and state.revision == 3
        # Legacy action endpoint must not bypass container rules.
        legacy = client.post(
            f"/api/role/actors/{run['player_id']}/object-actions",
            json={"request_id": "bypass", "object_id": box["id"], "expected_version": 1, "verb": "take"},
        )
        assert legacy.status_code == 400
        # Hidden identifiers cannot be used to probe or manipulate unknown contents.
        with db.session() as s:
            secret = next(
                i["id"]
                for i in s.get(ItemWorldState, run["story_id"]).state["items"]
                if i["definition"]["id"] == "paper-sheet"
            )
        probe = client.post(
            path + "/actions",
            json={
                "request_id": "probe",
                "item_id": secret,
                "expected_revision": 1,
                "action": "move",
                "destination": {"kind": "actor", "target": run["player_id"]},
            },
        )
        assert probe.status_code == 400
        assert (
            client.post(path + "/actions", json={**body, "action": "close", "destination": None}).status_code
            == 409
        )


def test_concurrent_actions_commit_once(db, config, fake):
    from concurrent.futures import ThreadPoolExecutor

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, run = start(client)
        path = f"/api/role/items/actors/{run['player_id']}"
        bag = client.get(path).json()["items"][0]

        def attempt(key):
            return client.post(
                path + "/actions",
                json={"request_id": key, "item_id": bag["id"], "expected_revision": 1, "action": "open"},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(attempt, ["a", "b"])) == [200, 409]
        with db.session() as s:
            assert s.get(ItemWorldState, run["story_id"]).revision == 2


def test_invalid_initial_world_rejected_before_save(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        value = recipe()
        value["item_world"]["actors"][0]["location"] = "wrong"
        response = client.put(
            "/api/role/experiments/blueprints/" + str(uuid4()), json={"expected_version": 0, "recipe": value}
        )
        assert response.status_code == 422
