"""Real production routes and projections, isolated experimental SQLite adapter."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from chatbot.items.catalog import instantiate
from chatbot.items.models import ActorState, Limit, Placement, World
from chatbot.items.persistence import initialize
from chatbot.items.runtime_api import runtime_router
from chatbot.observations import append_record, visible_records
from chatbot.runtime import Runtime
from chatbot.storage import (
    Conversation,
    ItemWorldState,
    RoleMemory,
    SceneRecord,
    Story,
    StoryEvent,
    Task,
    now,
)
from prototypes.windows_p0.sqlite_slice import SliceDatabase


def test_queue_migration_blockers_are_reproducible(tmp_path):
    """Expected failures document limits, not support for the existing worker."""
    db = SliceDatabase(tmp_path / "queue-blockers.sqlite3")
    Task.__table__.create(db.engine)
    seed(db)
    try:
        with db.session.begin() as s:
            s.add(
                Task(
                    id="queued",
                    owner_id="owner",
                    session_id="player",
                    kind="respond",
                    payload={},
                    deadline=now() + timedelta(minutes=2),
                )
            )
        runtime = Runtime(db, SimpleNamespace(lease_seconds=30))
        claimed = runtime.claim("probe")
        assert claimed.id == "queued" and claimed.status == "running"
        # SQLite DateTime round-trips naive values despite timezone=True.
        with pytest.raises(TypeError, match="offset-naive and offset-aware"):
            runtime.check(claimed.id, claimed.lease_version)
        # Background claims require an actual PostgreSQL advisory lock.
        with pytest.raises(OperationalError, match="pg_advisory_xact_lock"):
            runtime.claim("probe", background=True)
    finally:
        db.close()


def seed(db):
    with db.session.begin() as s:
        story = Story(id="story", owner_id="owner", title="P0 real slice")
        s.add(story)
        s.flush()
        people = {}
        for aid in ["player", "captain", "witness"]:
            actor = Conversation(
                id=aid,
                owner_id="owner",
                story_id=story.id,
                current_state={
                    "player": aid == "player",
                    "location": "pier",
                    "profile": {"name": aid},
                },
            )
            s.add(actor)
            people[aid] = actor
        s.flush()
        world = World(
            items=[instantiate("paper-sheet", "letter", Placement(kind="actor", target="player"))],
            actors=[
                ActorState(
                    id=aid,
                    location="pier",
                    lift_limit_g=Limit(mode="unlimited"),
                    carry_limit_g=Limit(mode="unlimited"),
                )
                for aid in people
            ],
        )
        initialize(s, story, world.model_dump(), people)
        secret = append_record(
            s,
            story_id=story.id,
            kind="speech",
            text="蓝鹭是暗号。船仍在码头。",
            actor=people["captain"],
            observer_ids=["captain", "witness"],
            channel="private",
            dedupe="private",
            perceptions={"witness": {"text": "船仍在码头。", "speaker_known": False}},
        )
        return secret.id


def client_for(db):
    app = FastAPI()
    app.include_router(runtime_router(db, SimpleNamespace(product="roleplay"), lambda: "owner"))
    return TestClient(app)


def test_real_item_api_replay_concurrency_and_restart(tmp_path):
    path = tmp_path / "slice.sqlite3"
    db = SliceDatabase(path)
    secret_id = seed(db)
    with client_for(db) as client:
        url = "/api/role/items/actors/player"
        item = client.get(url).json()["items"][0]
        body = {
            "request_id": "drop",
            "item_id": item["id"],
            "expected_revision": 1,
            "action": "move",
            "destination": {"kind": "location", "target": "pier"},
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: client.post(url + "/actions", json=body), range(2)))
        assert all(r.status_code == 200 for r in results), [r.text for r in results]
        assert results[0].json() == results[1].json()
        assert client.post(url + "/actions", json={**body, "request_id": "stale"}).status_code == 409
        assert client.post(url + "/actions", json={**body, "expected_revision": 2}).status_code == 409
        assert client.get("/api/role/items/actors/not-owned").status_code == 404
    db.close()
    db = SliceDatabase(path)
    try:
        with client_for(db) as client:
            assert client.post(url + "/actions", json=body).json() == results[0].json()
            assert client.get(url).json()["items"][0]["holder_id"] is None
        with db.session() as s:
            assert s.get(ItemWorldState, "story").revision == 2
            assert len(s.scalars(select(StoryEvent)).all()) == 1
            assert (
                len(s.scalars(select(SceneRecord).where(SceneRecord.dedupe == "item-action:drop")).all()) == 1
            )
            assert all(e.id != secret_id for e in visible_records(s, "player"))
            heard = next(e for e in visible_records(s, "witness") if e.id == secret_id)
            assert heard.text == "船仍在码头。" and heard.actor_id is None
            memories = [m for m in s.scalars(select(RoleMemory)) if secret_id in m.source_ids]
            assert {m.actor_id for m in memories} == {"captain", "witness"}
            assert any("蓝鹭" in m.content for m in memories if m.actor_id == "captain")
            assert all("蓝鹭" not in m.content for m in memories if m.actor_id == "witness")
    finally:
        db.close()


def test_real_append_rollback_and_unsupported_vector(tmp_path):
    db = SliceDatabase(tmp_path / "rollback.sqlite3")
    seed(db)
    try:
        with db.session() as s:
            before = s.get(Story, "story").seq
        with pytest.raises(RuntimeError, match="rollback"):
            with db.session.begin() as s:
                append_record(
                    s,
                    story_id="story",
                    kind="speech",
                    text="未提交的话",
                    actor=s.get(Conversation, "player"),
                    observer_ids=["player"],
                    dedupe="lost",
                )
                raise RuntimeError("rollback")
        with db.session() as s:
            assert s.get(Story, "story").seq == before
            assert not s.scalar(select(SceneRecord).where(SceneRecord.dedupe == "lost"))
            assert all("未提交的话" not in m.content for m in s.scalars(select(RoleMemory)))
        with pytest.raises(NotImplementedError, match="embedding"):
            with db.session.begin() as s:
                row = s.scalar(select(RoleMemory))
                assert row is not None
                row.embedding = [0.0] * 1024
    finally:
        db.close()
