from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from chatbot.api import create_app
from chatbot.experiment_initials import ExperimentStart, start
from chatbot.observations import append_record
from chatbot.storage import Conversation, ExperimentInitial, Story, Task


def test_templates_frozen_recipe_isolated_repeat_export(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        path = "/api/role/experiments"
        assert {t["template_id"] for t in client.get(path + "/templates").json()} == {
            "partial-hearing",
            "offstage-dialogue",
            "reply-lifecycle",
            "ferry-narrative",
        }
        body = {"request_id": "first", "template_id": "partial-hearing"}
        response = client.post(path + "/start", json=body)
        assert response.status_code == 201, response.text
        first = response.json()
        assert client.post(path + "/start", json=body).json() == first
        assert (
            client.post(path + "/start", json={**body, "template_id": "offstage-dialogue"}).status_code == 409
        )
        frame = f"/api/role/stories/{first['story_id']}/observation-frame"
        listener = client.get(frame, params={"actor_id": first["actors"]["restorer"]}).json()
        assert "钥匙" not in str(listener) and any(e["speaker"] == "未辨认的声音" for e in listener["events"])
        assert client.get(frame, params={"actor_id": first["player_id"]}).json()["events"] == []
        client.post(
            f"/api/role/actors/{first['actors']['restorer']}/memories",
            json={"kind": "goal", "content": "只属于第一次运行的新目标"},
        )
        exported = client.get(path + f"/stories/{first['story_id']}/initial").json()
        assert "只属于第一次" not in str(exported)
        assert "api_key" not in str(exported) and "base_url" not in str(exported)
        config.dialogue_model = "changed-model"
        second = client.post(
            path + "/start", json={"request_id": "repeat", "source_story_id": first["story_id"]}
        ).json()
        assert second["story_id"] != first["story_id"]
        assert set(second["actors"].values()).isdisjoint(first["actors"].values())
        assert second["recipe_hash"] == first["recipe_hash"] and second["initial_seq"] == first["initial_seq"]
        assert second["model_settings"]["dialogue_model"] == "changed-model"
        assert client.get(path + f"/stories/{first['story_id']}/initial").json() == exported
        assert "只属于第一次" not in str(
            client.get(f"/api/role/stories/{second['story_id']}/observation-frame").json()
        )
        with db.session() as s:
            assert s.get(Story, first["story_id"]).settings["paused"]
            assert s.get(Story, second["story_id"]).settings["paused"]
            assert s.scalar(select(func.count()).select_from(Task).where(Task.kind != "connection_check")) == 0
            assert all(t.status == "completed" for t in s.scalars(select(Task)))
        with db.session.begin() as s:
            s.get(ExperimentInitial, first["story_id"]).owner_id = "another-owner"
        assert client.get(path + f"/stories/{first['story_id']}/initial").status_code == 404
        assert (
            client.post(
                path + "/start", json={"request_id": "foreign", "source_story_id": first["story_id"]}
            ).status_code
            == 404
        )
        config.product = "office"
        assert client.get(path + "/templates").status_code == 404


def test_reply_lifecycle_template_exposes_per_actor_status_without_model_calls(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        result = client.post(
            "/api/role/experiments/start",
            json={"request_id": "reply-lifecycle", "template_id": "reply-lifecycle"},
        )
        assert result.status_code == 201, result.text
        run = result.json()
        story = run["story_id"]
        actors = run["actors"]
        path = f"/api/role/stories/{story}/observation-frame"

        matrix = client.get(path).json()["exchange_matrix"]
        assert len(matrix) == 1
        question = matrix[0]
        assert question["asker_id"] == actors["clerk"]
        assert question["target_id"] == actors["technician"]
        statuses = {view["actor_id"]: view["status"] for view in question["views"]}
        assert statuses == {
            actors["clerk"]: "open",
            actors["technician"]: "open",
            actors["witness"]: "open",
            actors["player"]: "unseen",
        }

        with db.session.begin() as s:
            technician = s.get(Conversation, actors["technician"])
            deferred = append_record(
                s,
                story_id=story,
                actor=technician,
                kind="speech",
                text="第六项仍需复查，我稍后答复。",
                observer_ids=[actors["clerk"], actors["technician"]],
                channel="private",
                dedupe="private-defer",
                payload={"exchange": {"reply_to": question["question_id"], "response_kind": "defer"}},
            )
            assert deferred.id
        statuses = {
            view["actor_id"]: view["status"]
            for view in client.get(path).json()["exchange_matrix"][0]["views"]
        }
        assert statuses[actors["clerk"]] == statuses[actors["technician"]] == "defer"
        assert statuses[actors["witness"]] == "open"
        assert statuses[actors["player"]] == "unseen"

        with db.session.begin() as s:
            append_record(
                s,
                story_id=story,
                actor=s.get(Conversation, actors["technician"]),
                kind="speech",
                text="第六项已经复查，我同意完成交接。",
                observer_ids=[actors["clerk"], actors["technician"], actors["witness"]],
                dedupe="public-answer",
                payload={"exchange": {"reply_to": question["question_id"], "response_kind": "answer"}},
            )
        statuses = {
            view["actor_id"]: view["status"]
            for view in client.get(path).json()["exchange_matrix"][0]["views"]
        }
        assert statuses == {
            actors["clerk"]: "answer",
            actors["technician"]: "answer",
            actors["witness"]: "answer",
            actors["player"]: "unseen",
        }
        assert not list(client.get(f"/api/role/stories/{story}/traces").json())


def test_template_start_concurrent_dedupe_and_rollback(db, config, runtime):
    config.product = "roleplay"
    body = ExperimentStart(request_id="concurrent", template_id="offstage-dialogue")

    def run():
        with db.session.begin() as s:
            return start(s, body, config.workspace_owner, config, runtime)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results[0] == results[1]
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(ExperimentInitial)) == 1
        assert s.scalar(select(func.count()).select_from(Story)) == 1
    # Failure after a story has been allocated must not leave a half-built experiment.
    config.context_hard_tokens = 1
    import pytest

    with pytest.raises(ValueError):
        with db.session.begin() as s:
            start(
                s,
                ExperimentStart(request_id="invalid", template_id="partial-hearing"),
                config.workspace_owner,
                config,
                runtime,
            )
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Story)) == 1
