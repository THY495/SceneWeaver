from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event
from sqlalchemy import select

from chatbot.api import create_app
from chatbot.storage import RunAnnotation, Story, Task, now


def test_run_notes_survive_reopen_and_retries_without_model_calls(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start",
            json={
                "request_id": "notes-run",
                "template_id": "offstage-dialogue",
            },
        ).json()
        sid = run["story_id"]
        path = f"/api/role/experiments/runs/{sid}"
        body = {"request_id": "note-1", "note": "先观察目标与知情", "task_id": None}
        first = client.post(path + "/notes", json=body)
        assert first.status_code == 201, first.text
        listed = client.get("/api/role/experiments/runs").json()["runs"]
        assert next(row for row in listed if row["id"] == sid)["note_count"] == 1
        assert client.post(path + "/notes", json=body).json() == first.json()
        assert client.post(path + "/notes", json={**body, "note": "changed"}).status_code == 409
        assert client.post(path + "/notes", json={**body, "note": " "}).status_code == 422
        assert client.get(path + "/decisions").json()["decisions"] == []
    with TestClient(create_app(config, db, fake)) as client:
        notes = client.get(path + "/notes").json()["notes"]
        assert len(notes) == 1 and notes[0]["note"] == body["note"]
        with db.session() as s:
            assert not list(s.scalars(select(Task).where(Task.kind != "connection_check")))
            assert len(list(s.scalars(select(RunAnnotation)))) == 1
        with db.session.begin() as s:
            s.get(Story, sid).owner_id = "different-owner"
        assert client.get(path + "/notes").status_code == 404
        assert client.get(path + "/decisions").status_code == 404
        assert client.post(path + "/notes", json=body).status_code == 404


def test_decision_notes_validate_scope_status_and_redaction(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start",
            json={
                "request_id": "decisions-run",
                "template_id": "offstage-dialogue",
            },
        ).json()
        sid = run["story_id"]
        path = f"/api/role/experiments/runs/{sid}"
        with db.session.begin() as s:
            task = Task(
                owner_id=config.workspace_owner,
                kind="npc_step",
                status="completed",
                deadline=now(),
                payload={"story_id": sid, "step_actor_id": next(iter(run["actors"].values()))},
                result={"experiment": {"outcome": "wait"}},
            )
            s.add(task)
            s.flush()
            tid = task.id
        body = {"request_id": "decision-note", "task_id": tid, "note": "本次等待，需要核对目标。"}
        assert client.post(path + "/notes", json=body).status_code == 201
        result = client.get(path + "/decisions").json()["decisions"][0]
        assert result["outcome"] == "wait" and result["events"] == []
        assert result["cost_usd"] == 0
        with db.session.begin() as s:
            s.get(Task, tid).status = "running"
        assert client.post(path + "/notes", json={**body, "request_id": "active"}).status_code == 409
        with db.session.begin() as s:
            t = s.get(Task, tid)
            t.status = "failed"
        assert client.post(path + "/notes", json={**body, "request_id": "failed"}).status_code == 201
        with db.session.begin() as s:
            s.get(Task, tid).payload = {"story_id": "another-story"}
        assert client.post(path + "/notes", json={**body, "request_id": "foreign"}).status_code == 404
        with db.session.begin() as s:
            t = s.get(Task, tid)
            t.payload = {"story_id": sid}
            t.result = {"redacted": True}
        assert client.get(path + "/notes").json()["notes"] == []
        assert client.get(path + "/decisions").json()["decisions"] == []
        assert client.post(path + "/notes", json=body).status_code == 404


def test_decision_list_batches_queries_as_history_grows(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        run = client.post(
            "/api/role/experiments/start",
            json={"request_id": "batched-decisions", "template_id": "offstage-dialogue"},
        ).json()
        sid = run["story_id"]
        actor_id = next(iter(run["actors"].values()))
        with db.session.begin() as s:
            s.add_all(
                Task(
                    owner_id=config.workspace_owner,
                    kind="npc_step",
                    status="completed",
                    deadline=now(),
                    payload={"story_id": sid, "step_actor_id": actor_id},
                    result={"experiment": {"outcome": "wait"}},
                )
                for _ in range(20)
            )
        statements = []

        def count(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)

        sql_event.listen(db.engine, "before_cursor_execute", count)
        try:
            response = client.get(f"/api/role/experiments/runs/{sid}/decisions")
        finally:
            sql_event.remove(db.engine, "before_cursor_execute", count)
        assert response.status_code == 200, response.text
        assert len(response.json()["decisions"]) == 20
        assert len(statements) <= 5, statements


def test_run_list_batches_initial_and_note_counts(db, config, fake):
    config.product = "roleplay"
    with db.session.begin() as s:
        stories = [
            Story(owner_id=config.workspace_owner, title=f"运行{i}", settings={"paused": True})
            for i in range(20)
        ]
        s.add_all(stories)
        s.flush()
        s.add_all(
            RunAnnotation(
                owner_id=config.workspace_owner,
                story_id=story.id,
                request_id=f"run-note-{index}",
                story_seq=0,
                note="批量读取验证",
            )
            for index, story in enumerate(stories)
        )
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    with TestClient(create_app(config, db, fake)) as client:
        sql_event.listen(db.engine, "before_cursor_execute", count)
        try:
            response = client.get("/api/role/experiments/runs")
        finally:
            sql_event.remove(db.engine, "before_cursor_execute", count)
    assert response.status_code == 200, response.text
    assert len(response.json()["runs"]) == 20
    assert all(row["note_count"] == 1 for row in response.json()["runs"])
    assert len(statements) <= 3, statements
