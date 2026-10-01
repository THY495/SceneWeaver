import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from support.story import launch

from chatbot.api import create_app
from chatbot.event_projection import backfill_story, projected_exchanges
from chatbot.observations import append_record, visible_records
from chatbot.scene_state import exchanges
from chatbot.storage import (
    Conversation,
    EventAuthority,
    InteractionRequest,
    RequestTransition,
    SpeechAct,
)


def test_private_reply_does_not_resolve_uninformed_observers_request(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        with db.session.begin() as s:
            a, b = [s.get(Conversation, x) for x in actors[:2]]
            question = append_record(
                s,
                story_id=story,
                actor=a,
                kind="speech",
                text="请回答。",
                observer_ids=[a.id, b.id, player],
                dedupe="question",
                payload={"exchange": {"expects_reply": True, "target_id": b.id}},
            )
            reply = append_record(
                s,
                story_id=story,
                actor=b,
                kind="speech",
                text="秘密答复。",
                observer_ids=[a.id, b.id],
                channel="private",
                dedupe="reply",
                payload={"exchange": {"reply_to": question.id, "response_kind": "answer"}},
            )
            assert s.get(EventAuthority, reply.id).source_class == "utterance"
            for viewer in [a.id, b.id, player]:
                assert projected_exchanges(s, viewer) == exchanges(visible_records(s, viewer))
            assert projected_exchanges(s, player)[0]["status"] == "open"
            assert projected_exchanges(s, a.id)[0]["status"] == "answer"
            qid, rid = question.id, reply.id
        assert client.delete(f"/api/role/stories/{story}/events/{rid}").status_code == 200
        with db.session() as s:
            assert projected_exchanges(s, actors[0])[0]["status"] == "open"
        assert client.delete(f"/api/role/stories/{story}/events/{qid}").status_code == 200
        with db.session() as s:
            assert projected_exchanges(s, actors[0]) == []


def test_backfill_is_bounded_repeatable_and_live_reply_repairs_old_request(db, config, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, _ = launch(client)
    with db.session.begin() as s:
        a, b = [s.get(Conversation, x) for x in actors[:2]]
        question = append_record(
            s,
            story_id=story,
            actor=a,
            kind="object_state",
            text="历史动作附带请求。",
            observer_ids=[a.id, b.id],
            dedupe="old",
            payload={"exchange": {"expects_reply": True, "target_id": b.id}},
        )
        s.flush()
        for table in [RequestTransition, InteractionRequest, SpeechAct, EventAuthority]:
            s.execute(delete(table))
        s.expire_all()
        reply = append_record(
            s,
            story_id=story,
            actor=b,
            kind="speech",
            text="稍后回答。",
            observer_ids=[a.id, b.id],
            dedupe="new",
            payload={"exchange": {"reply_to": question.id, "response_kind": "defer"}},
        )
        assert s.get(InteractionRequest, question.id)
        assert s.get(SpeechAct, question.id) is None
        watermark = reply.seq
        for _ in range(2):
            after = 0
            while after < watermark:
                page = backfill_story(s, story, after=after, through=watermark, limit=1)
                assert page["processed"] == 1
                after = page["after"]
        assert s.scalar(select(func.count()).select_from(RequestTransition)) == 1
        assert projected_exchanges(s, a.id) == exchanges(visible_records(s, a.id))


def test_projection_migration_roundtrip(db):
    path = Path("migrations/versions/007_typed_event_indices.py")
    spec = importlib.util.spec_from_file_location("migration007", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with db.engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        migration.downgrade()
        migration.upgrade()
        for table in [EventAuthority, SpeechAct, InteractionRequest, RequestTransition]:
            assert conn.scalar(select(func.count()).select_from(table)) == 0
