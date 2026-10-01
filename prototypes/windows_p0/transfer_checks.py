"""HTTP-only transfer checks; author API injects consent, not a model decision."""

import sys
import time
from pathlib import Path
from uuid import uuid4

# Only create input fixtures from local pure models; never open the host DB.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Limit, Placement, World
from chatbot.narrative_demo import ferry_story


def verify_transfers(client):
    def request(method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    outcomes = []
    for decision in ("accept", "refuse"):
        recipe = ferry_story()
        recipe.pop("narrative")
        recipe["people"] = recipe["people"][:1]
        recipe["item_world"] = World(
            items=[instantiate("paper-sheet", "letter", Placement(kind="actor", target="player"))],
            actors=[
                ActorState(
                    id=p["alias"],
                    location="候船亭",
                    lift_limit_g=Limit(mode="unlimited"),
                    carry_limit_g=Limit(mode="unlimited"),
                )
                for p in [recipe["player"], *recipe["people"]]
            ],
        ).model_dump()
        bid = str(uuid4())
        request(
            "PUT", f"/api/role/experiments/blueprints/{bid}", json={"expected_version": 0, "recipe": recipe}
        )
        run = request(
            "POST",
            "/api/role/experiments/start",
            json={"request_id": bid, "blueprint_id": bid, "blueprint_version": 1},
        )
        request(
            "PUT",
            f"/api/role/stories/{run['story_id']}/settings",
            json={"paused": False, "idle_enabled": False},
        )
        pid, npc = run["player_id"], run["actors"]["captain"]
        path = f"/api/role/items/actors/{pid}"
        item = request("GET", path)["items"][0]
        offer_body = {
            "request_id": str(uuid4()),
            "item_id": item["id"],
            "expected_revision": 1,
            "recipient_id": npc,
        }
        offer = request("POST", path + "/offers", json=offer_body)
        assert request("POST", path + "/offers", json=offer_body) == offer
        # Let the real Worker finish its acknowledgement; no SQL task overrides.
        deadline = time.monotonic() + 20
        while True:
            session = request("GET", f"/api/sessions/{pid}")
            task = next(t for t in session["tasks"] if t["id"] == offer["task_id"])
            if task["status"] == "completed":
                break
            assert task["status"] not in {"failed", "superseded"}, task
            if time.monotonic() > deadline:
                raise TimeoutError("Offer reply did not complete")
            time.sleep(0.1)
        inventory = request("GET", path)
        assert inventory["items"][0]["holder_id"] == pid
        transfer = inventory["transfers"][0]
        assert transfer["state"] == "wait_reply"
        action = {
            "request_id": str(uuid4()),
            "action": "give",
            "item_id": transfer["object_id"],
            "expected_revision": 1,
            "destination": {"kind": "actor", "target": npc},
            "agreement": {k: transfer[k] for k in ("proposal_id", "version", "item_id")},
        }
        assert client.post(path + "/actions", json=action).status_code == 409
        request(
            "POST",
            f"/api/role/actors/{npc}/agreements/{transfer['proposal_id']}/decisions",
            json={
                "request_id": str(uuid4()),
                "expected_version": 1,
                "item_ids": [transfer["item_id"]],
                "decision": decision,
            },
        )
        result = client.post(path + "/actions", json=action)
        if decision == "accept":
            assert result.status_code == 200, result.text
            assert request("POST", path + "/actions", json=action) == result.json()
            assert client.post(path + "/actions", json={**action, "expected_revision": 2}).status_code == 409
        else:
            assert result.status_code == 409, result.text
        snapshot = request("GET", f"/api/role/players/{pid}/snapshot")
        holder = npc if decision == "accept" else pid
        assert snapshot["inventory"]["items"][0]["holder_id"] == holder
        outcomes.append(
            {
                "player_id": pid,
                "holder_id": holder,
                "decision": decision,
                "transfer_state": snapshot["inventory"]["transfers"][0]["state"],
            }
        )
    return outcomes
