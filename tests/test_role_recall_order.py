"""Database distance order must survive projection to an actor's visible sources."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch

from chatbot.api import create_app
from chatbot.observations import append_record, visible_records
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, Operation, RoleMemory, RoleSnapshot, uid


def vector(x, y=0):
    return [x, y] + [0.0] * 1022


def snapshot(db, actor_id):
    with db.session.begin() as session:
        actor = session.get(Conversation, actor_id)
        records = visible_records(session, actor_id)
        session.add(RoleSnapshot(id=uid(), actor_id=actor_id,
                                 covered_seq=max((record.seq for record in records), default=0),
                                 privacy_version=actor.privacy_version,
                                 source_ids=[record.id for record in records], summary={}))


def query(db, config, runtime, player, *, key):
    task = runtime.submit(player, config.workspace_owner, "如何防潮？", key)
    with db.session.begin() as session:
        session.add(Operation(id=f"{task.id}:role-query", task_id=task.id,
                              payload={"vector": vector(1)}))
    return task


def index_source(db, config, actor, source, embedding):
    with db.session.begin() as session:
        row = session.scalar(select(RoleMemory).where(
            RoleMemory.actor_id == actor, RoleMemory.kind == "episode",
            RoleMemory.source_ids.contains([source])))
        assert row
        row.embedding, row.embedding_model = embedding, config.embedding_model


def test_nearest_archive_source_precedes_older_distant_sources(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    config.embedding_provider = "api"
    sources = []
    # The nearest record is intentionally newest. Chronological ordering is
    # correct for dialogue, but must not replace retrieval relevance ordering.
    for i, embedding in enumerate([vector(0, 1), vector(.6, .8), vector(.8, .6), vector(1)]):
        with db.session.begin() as session:
            source = append_record(session, story_id=story, kind="speech",
                                   actor=session.get(Conversation, player), text=f"备查记录甲第{i}项。",
                                   channel="private", addressees=[actors[0]], observer_ids=[actors[0]],
                                   dedupe=f"recall-order-{i}").id
        index_source(db, config, actors[0], source, embedding)
        sources.append(source)
    snapshot(db, actors[0])
    task = query(db, config, runtime, player, key="rank-query")
    _, info = RoleMemoryService(db, config, runtime, fake).context(task, actors[0])
    retrieved = [record["id"] for record in info["observations"] if record["id"] in sources]
    assert retrieved == list(reversed(sources))


@pytest.mark.parametrize("multiple_sources", [False, True])
def test_ranked_cutoff_preserves_lexical_prefix_duplicates_and_pinned_source(
    db, config, runtime, fake, multiple_sources,
):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    config.embedding_provider = "api"
    lexical, semantic = [], []
    for i, text in enumerate(["防潮办法甲", "防潮办法乙", "防潮办法丙", *[f"备查记录第{j}项" for j in range(4)]]):
        with db.session.begin() as session:
            source = append_record(session, story_id=story, kind="speech",
                                   actor=session.get(Conversation, player), text=text,
                                   channel="private", addressees=[actors[0]], observer_ids=[actors[0]],
                                   dedupe=f"cutoff-{i}").id
        (lexical if i < 3 else semantic).append(source)
    for source, embedding in zip(semantic, [vector(0, 1), vector(.6, .8), vector(.8, .6), vector(1)], strict=True):
        index_source(db, config, actors[0], source, embedding)
    with db.session.begin() as session:
        if multiple_sources:
            nearest = session.scalar(select(RoleMemory).where(
                RoleMemory.actor_id == actors[0], RoleMemory.kind == "episode",
                RoleMemory.source_ids.contains([semantic[-1]])))
            nearest.source_ids = [lexical[0], semantic[-1], semantic[-1], semantic[-2]]
        session.add(RoleMemory(id=uid(), story_id=story, actor_id=actors[0], kind="commitment",
                               content="我还要核对最早那份记录。", source_ids=[semantic[0]],
                               context_source_ids=[semantic[0]]))
    snapshot(db, actors[0])
    task = query(db, config, runtime, player, key="cutoff-query")
    _, info = RoleMemoryService(db, config, runtime, fake).context(task, actors[0])
    all_sources = set(lexical + semantic)
    retrieved = [record["id"] for record in info["observations"] if record["id"] in all_sources]
    assert retrieved == [*lexical, *reversed(semantic[1:]), semantic[0]]
    assert len(retrieved) == len(set(retrieved))


def test_semantic_source_projection_keeps_partial_hearing_and_deletion(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
        config.embedding_provider = "api"
        keeper, listener, absent = actors
        partial = "步骤在绿盒内"
        with db.session.begin() as session:
            source = append_record(
                session, story_id=story, kind="speech", actor=session.get(Conversation, player),
                text=partial + "，暗号四一七。", channel="private", addressees=[keeper],
                observer_ids=[keeper, listener],
                perceptions={listener: {"text": partial, "speaker_known": False}},
                dedupe="rank-partial-source").id
        for actor in (keeper, listener):
            index_source(db, config, actor, source, vector(1))
            snapshot(db, actor)
        snapshot(db, absent)
        task = query(db, config, runtime, player, key="partial-query")
        service = RoleMemoryService(db, config, runtime, fake)
        _, info = service.context(task, listener)
        heard = next(record for record in info["observations"] if record["id"] == source)
        assert heard["text"] == partial and heard["actor_id"] is None
        assert heard["speaker"] == "未辨认的声音"
        _, absent_info = service.context(task, absent)
        assert source not in {record["id"] for record in absent_info["observations"]}
        response = client.delete(f"/api/role/stories/{story}/events/{source}")
        assert response.status_code == 200
        after = query(db, config, runtime, player, key="deleted-query")
        for actor in (keeper, listener, absent):
            _, latest = service.context(after, actor)
            assert source not in {record["id"] for record in latest["observations"]}
