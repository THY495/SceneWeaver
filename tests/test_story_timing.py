from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch

from chatbot.api import create_app
from chatbot.storage import Operation, Task, now


def test_story_clock_links_nested_background_without_exporting_payloads(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, _, player = launch(client)
        root = runtime.submit(player, config.workspace_owner, "clock test", "clock-test")
        with db.session.begin() as s:
            for tid, parent, owner in (
                ("bg1", root.id, config.workspace_owner),
                ("bg2", "bg1", config.workspace_owner),
                ("foreign", root.id, "other-owner"),
            ):
                s.add(
                    Task(
                        id=tid,
                        parent_id=parent,
                        owner_id=owner,
                        kind="memory",
                        status="queued",
                        payload={"secret": "never-export"},
                        deadline=now() + timedelta(minutes=1),
                    )
                )
            s.add(
                Operation(
                    id="timing:bg2:test",
                    task_id="bg2",
                    payload={
                        "timing_v1": {
                            "id": "trace",
                            "task_id": "bg2",
                            "started_at": now().isoformat(),
                            "duration_ms": 12,
                            "spans": [],
                            "requests": [{"private": "never-export"}],
                        }
                    },
                )
            )
        response = client.get(f"/api/role/stories/{sid}/timing-overview")
        assert response.status_code == 200
        body = response.json()
        ids = {t["id"] for t in body["tasks"]}
        assert {root.id, "bg1", "bg2"} <= ids
        assert "foreign" not in ids
        assert "never-export" not in response.text
        assert body["traces"][0]["task_id"] == "bg2"
        assert not any(body["truncated"].values())
        assert any(t["status"] == "queued" for t in body["tasks"])
        other, _, _ = launch(client)
        assert not client.get(f"/api/role/stories/{other}/timing-overview").json()["tasks"]


def test_roleplay_skips_personal_memory_queue_and_overdue_is_explicit(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, _, player = launch(client)
        role_task = runtime.submit(player, config.workspace_owner, "我选音乐", "role-no-personal")
        with db.session.begin() as s:
            assert not s.scalar(select(Task).where(Task.parent_id == role_task.id, Task.kind == "memory"))
            s.add(
                Task(
                    id="expired-memory",
                    owner_id=config.workspace_owner,
                    parent_id=role_task.id,
                    kind="memory",
                    status="queued",
                    payload={},
                    deadline=now() - timedelta(seconds=1),
                )
            )
        data = client.get(f"/api/role/stories/{sid}/timing-overview").json()
        assert next(t for t in data["tasks"] if t["id"] == "expired-memory")["overdue"]
    personal = runtime.create_session(config.workspace_owner)
    task = runtime.submit(personal.id, config.workspace_owner, "我的长期偏好", "personal-memory")
    with db.session() as s:
        assert s.scalar(select(Task).where(Task.parent_id == task.id, Task.kind == "memory"))
