"""Experimental adapter for one real item/event API slice, not an application DB.

Import only in dedicated probes/tests. SQLite serializes ALL transactions here;
FOR UPDATE is not claimed to work. Vector columns are nullable placeholders and
semantic search / PostgreSQL worker / checkpoints are explicitly unsupported.
"""

from pathlib import Path

from pgvector.sqlalchemy import Vector
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from chatbot.storage import (
    AgreementEvent,
    AgreementNotice,
    Base,
    Conversation,
    EventAuthority,
    InteractionRequest,
    ItemWorldState,
    Observation,
    RequestTransition,
    RoleMemory,
    SceneRecord,
    SpeechAct,
    Story,
    StoryEvent,
)


@compiles(JSONB, "sqlite")
def json_type(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(Vector, "sqlite")
def vector_type(_type, _compiler, **_kwargs):
    return "TEXT"


class SliceDatabase:
    def __init__(self, path: Path):
        self.engine = create_engine("sqlite:///" + str(path.resolve()), connect_args={"timeout": 10})

        @event.listens_for(self.engine, "connect")
        def configure(connection, _record):
            connection.isolation_level = None
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")

        @event.listens_for(self.engine, "begin")
        def begin(connection):
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        self.session = sessionmaker(self.engine, expire_on_commit=False)

        @event.listens_for(self.session, "before_flush")
        def reject_unimplemented(session, _context, _instances):
            for row in session.new | session.dirty:
                if isinstance(row, RoleMemory) and row.embedding is not None:
                    raise NotImplementedError("P0 slice does not support embedding writes or vector search")

        classes = [
            Story,
            Conversation,
            SceneRecord,
            Observation,
            RoleMemory,
            EventAuthority,
            SpeechAct,
            InteractionRequest,
            RequestTransition,
            AgreementEvent,
            AgreementNotice,
            ItemWorldState,
            StoryEvent,
        ]
        Base.metadata.create_all(self.engine, tables=[cls.__table__ for cls in classes])

    def close(self):
        self.engine.dispose()
