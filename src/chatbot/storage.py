from datetime import UTC, datetime
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Index, String, Text, UniqueConstraint, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .config import Settings


def now() -> datetime:
    return datetime.now(UTC)


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Story(Base):
    __tablename__ = "stories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(160))
    world: Mapped[str] = mapped_column(Text, default="")
    seq: Mapped[int] = mapped_column(default=0)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ItemWorldState(Base):
    __tablename__ = "item_world_states"
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(default=1)
    state: Mapped[dict] = mapped_column(JSONB)


class StoryEvent(Base):
    __tablename__ = "story_events"
    __table_args__ = (UniqueConstraint("story_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class StoryBlueprint(Base):
    __tablename__ = "story_blueprints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[int] = mapped_column(default=1)
    recipe: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ExperimentInitial(Base):
    """Immutable experiment recipe and creation receipt, separate from mutable world state."""

    __tablename__ = "experiment_initials"
    __table_args__ = (UniqueConstraint("owner_id", "request_id"),)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    request_id: Mapped[str] = mapped_column(String(80))
    request_hash: Mapped[str] = mapped_column(String(64))
    recipe: Mapped[dict] = mapped_column(JSONB)
    receipt: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RunAnnotation(Base):
    __tablename__ = "run_annotations"
    __table_args__ = (UniqueConstraint("owner_id", "story_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    request_id: Mapped[str] = mapped_column(String(80))
    story_seq: Mapped[int] = mapped_column()
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventAnnotation(Base):
    __tablename__ = "event_annotations"
    __table_args__ = (UniqueConstraint("owner_id", "event_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(80))
    source_hash: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(30))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ExperimentReview(Base):
    __tablename__ = "experiment_reviews"
    __table_args__ = (UniqueConstraint("owner_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    left_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    right_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(80))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SceneRecord(Base):
    __tablename__ = "scene_records"
    __table_args__ = (UniqueConstraint("story_id", "seq"), UniqueConstraint("story_id", "dedupe"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    seq: Mapped[int | None] = mapped_column(nullable=True)
    kind: Mapped[str] = mapped_column(String(24))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(100), default="")
    text: Mapped[str] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(24), default="public")
    scene: Mapped[str] = mapped_column(String(100), default="main")
    addressees: Mapped[list] = mapped_column(JSONB, default=list)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    dedupe: Mapped[str] = mapped_column(String(160))
    occurred_at: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    redacted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Observation(Base):
    __tablename__ = "role_observations"
    actor_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), primary_key=True)
    perception: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class EventAuthority(Base):
    """Typed index of event authority, never an assertion extracted by a model."""

    __tablename__ = "event_authority"
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), primary_key=True)
    source_class: Mapped[str] = mapped_column(String(32), index=True)


class SpeechAct(Base):
    """Speech identity; original content and visibility remain owned by the event."""

    __tablename__ = "speech_acts"
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), primary_key=True)
    speaker_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True, index=True)


class InteractionRequest(Base):
    """Stable request identity, separate from the state visible to each observer."""

    __tablename__ = "interaction_requests"
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), primary_key=True)
    asker_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)


class RequestTransition(Base):
    __tablename__ = "request_transitions"
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("interaction_requests.event_id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(24))


class AgreementEvent(Base):
    __tablename__ = "agreement_events"
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), primary_key=True)
    proposal_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int]
    kind: Mapped[str] = mapped_column(String(24))


class AgreementNotice(Base):
    __tablename__ = "agreement_notices"
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    notification_id: Mapped[int | None] = mapped_column(nullable=True)


class RoleMemory(Base):
    __tablename__ = "role_memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    content: Mapped[str] = mapped_column(Text)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(24), default="active")
    version: Mapped[int] = mapped_column(default=1)
    replaces_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    embedding: Mapped[object | None] = mapped_column(Vector(1024), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class RoleSnapshot(Base):
    __tablename__ = "role_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    covered_seq: Mapped[int]
    privacy_version: Mapped[int]
    summary: Mapped[dict] = mapped_column(JSONB)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Conversation(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    story_id: Mapped[str | None] = mapped_column(ForeignKey("stories.id"), index=True, nullable=True)
    title: Mapped[str] = mapped_column(String(160), default="新对话")
    epoch: Mapped[int] = mapped_column(default=0)
    seq: Mapped[int] = mapped_column(default=0)
    revision: Mapped[int] = mapped_column(default=0)
    privacy_version: Mapped[int] = mapped_column(default=0)
    current_state: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlayerReading(Base):
    __tablename__ = "player_reading"
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    epoch: Mapped[int] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(default=0)
    progress: Mapped[dict] = mapped_column(JSONB, default=dict)


class CharacterCard(Base):
    __tablename__ = "character_cards"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[int] = mapped_column(default=1)
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class CharacterRevision(Base):
    __tablename__ = "character_revisions"
    card_id: Mapped[str] = mapped_column(ForeignKey("character_cards.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    seq: Mapped[int]
    kind: Mapped[str] = mapped_column(String(40))
    epoch: Mapped[int]
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("session_id", "client_id"), UniqueConstraint("task_id", "role"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    client_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    seq: Mapped[int]
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    redacted: Mapped[bool] = mapped_column(default=False)
    delivered: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), index=True, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    epoch: Mapped[int] = mapped_column(default=0)
    priority: Mapped[int] = mapped_column(default=50)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_version: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(default=0)
    calls: Mapped[int] = mapped_column(default=0)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)


class ActionAttempt(Base):
    __tablename__ = "action_attempts"
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    task_lease_version: Mapped[int]
    proposal_id: Mapped[str] = mapped_column(String(36), index=True)
    proposal_version: Mapped[int]
    item_id: Mapped[str] = mapped_column(String(36), index=True)
    command_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="reserved", index=True)
    pause_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Operation(Base):
    __tablename__ = "operations"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GameSave(Base):
    __tablename__ = "game_saves"
    __table_args__ = (UniqueConstraint("owner_id", "release_id", "request_id"),)
    player_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    story_id: Mapped[str] = mapped_column(ForeignKey("stories.id"), unique=True)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    release_id: Mapped[str] = mapped_column(String(64), index=True)
    request_id: Mapped[str] = mapped_column(String(80))
    label: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    name: Mapped[str] = mapped_column(String(250))
    version: Mapped[int] = mapped_column(default=1)
    content_hash: Mapped[str] = mapped_column(String(64))
    blob_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version: Mapped[int]
    ordinal: Mapped[int]
    section: Mapped[str] = mapped_column(String(250))
    content: Mapped[str] = mapped_column(Text)
    terms: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[object | None] = mapped_column(TSVECTOR, nullable=True)
    embedding: Mapped[object | None] = mapped_column(Vector(1024), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    __table_args__ = (
        Index("ix_chunks_search", "search_vector", postgresql_using="gin"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id"))
    document_version: Mapped[int]
    excerpt: Mapped[str] = mapped_column(Text)


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    subject: Mapped[str] = mapped_column(String(100), default="user")
    predicate: Mapped[str] = mapped_column(String(100))
    value: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(100), default="general")
    status: Mapped[str] = mapped_column(String(30), default="user_asserted")
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Snapshot(Base):
    __tablename__ = "context_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    covered_seq: Mapped[int]
    privacy_version: Mapped[int]
    summary: Mapped[str] = mapped_column(Text)
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(20), default="ready")
    token_count: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Tombstone(Base):
    __tablename__ = "tombstones"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    target_id: Mapped[str] = mapped_column(String(36))
    source_ids: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Usage(Base):
    __tablename__ = "usage"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(30))
    model: Mapped[str] = mapped_column(String(100))
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cache_tokens: Mapped[int] = mapped_column(default=0)
    cost: Mapped[float] = mapped_column(Float, default=0)
    reserved: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(30), default="reserved")
    duration_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VoiceDelivery(Base):
    __tablename__ = "voice_deliveries"
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    text_visible: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class VoicePlayback(Base):
    __tablename__ = "voice_playbacks"
    __table_args__ = (UniqueConstraint("session_id", "request_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    event_id: Mapped[str] = mapped_column(ForeignKey("scene_records.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    request_id: Mapped[str] = mapped_column(String(80))
    chunk_index: Mapped[int]
    epoch: Mapped[int]
    text_hash: Mapped[str] = mapped_column(String(64))
    voice: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    sent_samples: Mapped[int] = mapped_column(default=0)
    played_samples: Mapped[int] = mapped_column(default=0)
    receipt_seq: Mapped[int] = mapped_column(default=0)
    stream_done: Mapped[bool] = mapped_column(default=False)
    timings: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Database:
    def __init__(self, config: Settings):
        self.engine = create_engine(
            config.database_url,
            pool_pre_ping=True,
            pool_size=8,
            max_overflow=4,
            connect_args={"connect_timeout": 5},
        )
        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self):
        with self.engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(self.engine)
        # Frozen Windows hosts initialize existing player databases directly;
        # they do not carry the repository's Alembic command-line environment.
        with self.engine.begin() as conn:
            conn.execute(text("ALTER TABLE scene_records ALTER COLUMN seq DROP NOT NULL"))

    def close(self):
        self.engine.dispose()
