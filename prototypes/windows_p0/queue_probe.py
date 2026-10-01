"""Isolated UTC/queue experiment. Never import into the production application.

The advisory bridge is valid ONLY because every transaction already holds the
database-wide SQLite write reservation; it is not an implementation of PG locks.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import DateTime, event, select
from sqlalchemy.types import TypeDecorator
from sqlite_slice import SliceDatabase

from chatbot.runtime import Runtime, StaleTask
from chatbot.storage import Base, Conversation, Event, Message, Task, now


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, _dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("UTC experiment requires timezone-aware writes")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value, _dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


def run(path):
    # Process-local metadata adaptation. Keeps the original PostgreSQL type.
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime) and column.type.timezone:
                column.type = column.type.with_variant(UTCDateTime(), "sqlite")
    db = SliceDatabase(path)

    @event.listens_for(db.engine, "connect")
    def bridge(connection, _record):
        def locked(key):
            if not connection.in_transaction or key != 7831001:
                raise ValueError("Unsupported advisory operation or missing serialized transaction")
            return 1

        connection.create_function("pg_advisory_xact_lock", 1, locked)

    db.engine.dispose()
    Base.metadata.create_all(db.engine, tables=[Task.__table__, Event.__table__, Message.__table__])
    runtime = Runtime(db, SimpleNamespace(lease_seconds=30, task_timeout=120))
    checks = []
    try:
        actor = runtime.create_session("owner", "queue-probe")
        with db.session.begin() as s:
            queued = runtime.enqueue(s, "respond", "owner", {}, session_id=actor.id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(runtime.claim, ["one", "two"]))
        claimed = next(task for task in claims if task is not None)
        assert sum(task is not None for task in claims) == 1
        assert runtime.check(claimed.id, claimed.lease_version).deadline.tzinfo is not None
        assert runtime.heartbeat(claimed.id, claimed.lease_version)
        checks.append("two concurrent workers claim a task exactly once; UTC check and heartbeat pass")

        # Expire lease; a new owner may resume, previous owner may not finish.
        with db.session.begin() as s:
            s.get(Task, queued.id).lease_until = now() - timedelta(seconds=1)
        resumed = runtime.claim("replacement")
        assert resumed.lease_version == claimed.lease_version + 1
        assert not runtime.heartbeat(claimed.id, claimed.lease_version)
        assert not runtime.finish(claimed.id, claimed.lease_version, {"answer": "late"})
        assert runtime.finish(resumed.id, resumed.lease_version, {"answer": "fixture reply"})
        assert not runtime.finish(resumed.id, resumed.lease_version, {"answer": "duplicate"})
        with db.session() as s:
            messages = s.scalars(select(Message).where(Message.task_id == resumed.id)).all()
            assert len(messages) == 1 and messages[0].content == "fixture reply"
        checks.append("lease takeover rejects late owner; completion and message commit once")

        with db.session.begin() as s:
            runtime.enqueue(s, "respond", "owner", {}, session_id=actor.id)
        stale = runtime.claim("old-input")
        with db.session.begin() as s:
            s.get(Conversation, actor.id).epoch += 1
        try:
            runtime.check(stale.id, stale.lease_version)
            raise AssertionError("old epoch should fail")
        except StaleTask:
            pass
        assert not runtime.finish(stale.id, stale.lease_version, {"answer": "obsolete"})
        with db.session() as s:
            assert s.get(Task, stale.id).status == "superseded"
        checks.append("input epoch change rejects obsolete completion")

        with db.session.begin() as s:
            background = runtime.enqueue(s, "memory", "owner", {}, session_id=actor.id, epoch=1)
        taken = runtime.claim("background", background=True)
        assert taken.id == background.id
        assert runtime.finish(taken.id, taken.lease_version, {"fixture": True})
        checks.append("background claim uses checked whole-database serialization bridge")
        return {
            "checks": checks,
            "status": "experimental_pass",
            "limitations": [
                "all transactions serialize, including reads",
                "single database coordinator semantics only",
                "no real model, vector search, checkpoint or full NPC scene run",
                "not frozen or clean-machine tested",
            ],
        }
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    assert not args.database.exists(), "Use a fresh experiment database"
    args.database.parent.mkdir(parents=True, exist_ok=True)
    result = run(args.database)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
