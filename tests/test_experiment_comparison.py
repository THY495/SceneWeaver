from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from chatbot.api import create_app
from chatbot.storage import ExperimentReview, Story, Task, Usage


def start(client, rid, template="partial-hearing"):
    response = client.post("/api/role/experiments/start", json={"request_id": rid, "template_id": template})
    assert response.status_code == 201, response.text
    return response.json()


def review_body(comparison):
    return {
        "request_id": "review-1",
        "left_id": comparison["left"]["record"]["story"]["id"],
        "right_id": comparison["right"]["record"]["story"]["id"],
        "left_signature": comparison["left"]["signature"],
        "right_signature": comparison["right"]["signature"],
        "left_ratings": {"information_boundary": "pass"},
        "right_ratings": {"information_boundary": "uncertain"},
        "note": "人工观察：匿名片段未泄露钥匙位置。",
        "left_evidence": [],
        "right_evidence": [],
    }


def test_compare_review_binding_idempotence_and_withdrawal(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        a, b = start(client, "a"), start(client, "b")
        path = "/api/role/experiments"
        params = {"left_id": a["story_id"], "right_id": b["story_id"]}
        comparison = client.get(path + "/compare", params=params).json()
        assert comparison["same_initial"] and comparison["same_initial_model_settings"]
        assert (
            comparison["left"]["signature"]
            == client.get(path + "/compare", params=params).json()["left"]["signature"]
        )
        body = review_body(comparison)
        speech = next(e for e in comparison["left"]["record"]["events"] if e["kind"] == "speech")
        body["left_evidence"] = [speech["id"]]
        saved = client.post(path + "/reviews", json=body)
        assert saved.status_code == 201, saved.text
        assert client.post(path + "/reviews", json=body).json()["id"] == saved.json()["id"]
        assert client.post(path + "/reviews", json={**body, "note": "changed"}).status_code == 409
        assert (
            client.post(
                path + "/reviews",
                json={**body, "request_id": "bad-evidence", "right_evidence": [speech["id"]]},
            ).status_code
            == 422
        )
        client.post(
            f"/api/role/actors/{b['actors']['restorer']}/memories",
            json={"kind": "goal", "content": "新的实验条件"},
        )
        assert client.post(path + "/reviews", json={**body, "request_id": "stale"}).status_code == 409
        assert (
            client.get(path + "/reviews", params=params).json()["reviews"][0]["annotation"]["note"]
            == body["note"]
        )
        assert client.delete(f"/api/role/stories/{a['story_id']}/events/{speech['id']}").status_code == 200
        withdrawn = client.get(path + "/reviews", params=params).json()["reviews"][0]
        assert withdrawn["source_withdrawn"] and withdrawn["annotation"] is None
        assert body["note"] not in str(withdrawn)
        after = client.get(path + "/compare", params=params).json()
        assert after["same_initial"] is None
        with db.session() as s:
            assert s.scalar(select(func.count()).select_from(ExperimentReview)) == 1
            assert s.scalar(
                select(func.count()).select_from(Task).where(Task.kind != "connection_check")
            ) == 0


def test_compare_mismatched_start_access_and_late_cost(db, config, fake, runtime):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        a, b = start(client, "a"), start(client, "b", "offstage-dialogue")
        path = "/api/role/experiments"
        params = {"left_id": a["story_id"], "right_id": b["story_id"]}
        comparison = client.get(path + "/compare", params=params).json()
        assert comparison["same_initial"] is False
        assert (
            client.get(
                path + "/compare", params={"left_id": a["story_id"], "right_id": a["story_id"]}
            ).status_code
            == 422
        )
        with db.session.begin() as s:
            task = runtime.enqueue(s, "npc_step", config.workspace_owner, {"story_id": a["story_id"]})
            task.status = "completed"
        current = client.get(path + "/compare", params=params).json()
        body = review_body(current)
        with db.session.begin() as s:
            s.add(
                Usage(
                    owner_id=config.workspace_owner,
                    task_id=task.id,
                    role="dialogue",
                    model="other-model",
                    cost=0.01,
                    status="completed",
                )
            )
        assert client.post(path + "/reviews", json=body).status_code == 409
        refreshed = client.get(path + "/compare", params=params).json()
        assert refreshed["left"]["summary"]["actual_models"] == ["other-model"]
        with db.session.begin() as s:
            s.get(Story, b["story_id"]).owner_id = "foreign"
        assert client.get(path + "/compare", params=params).status_code == 404
        assert client.get(path + "/reviews", params=params).status_code == 404
        assert client.post(path + "/reviews", json=body).status_code == 404
        config.product = "office"
        assert client.get(path + "/compare", params=params).status_code == 404


def test_concurrent_review_requests_return_same_receipt(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        a, b = start(client, "a"), start(client, "b")
        path = "/api/role/experiments"
        data = client.get(
            path + "/compare", params={"left_id": a["story_id"], "right_id": b["story_id"]}
        ).json()
        body = review_body(data)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(path + "/reviews", json=body), range(2)))
        assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
        assert responses[0].json()["id"] == responses[1].json()["id"]
        with db.session() as s:
            assert s.scalar(select(func.count()).select_from(ExperimentReview)) == 1
