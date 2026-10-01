from fastapi.testclient import TestClient
from sqlalchemy import select, update
from support.story import launch

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.role_index import index_memories, schedule_index
from chatbot.storage import Conversation, RoleMemory, Task


async def test_index_queue_is_bounded_idempotent_and_rechecks_privacy(db, config, runtime, fake, monkeypatch):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
        runtime.submit(
            player,
            config.workspace_owner,
            "这是我的私下发言",
            "private",
            channel="private",
            addressees=[actors[0]],
        )
    config.embedding_provider = "openai"
    task = schedule_index(db, config, runtime)
    assert task and schedule_index(db, config, runtime) is None
    with db.session.begin() as s:
        s.execute(update(Task).where(Task.id != task.id).values(status="cancelled"))
    job = runtime.claim("index", background=True)
    seen = []

    async def encode(self, texts, task_id):
        seen.extend(texts)
        with db.session.begin() as s:
            s.get(Conversation, job.session_id).privacy_version += 1
        return [[0.1] * 1024 for _ in texts]

    monkeypatch.setattr("chatbot.role_index.Embeddings.encode", encode)
    result = await index_memories(Agents(db, config, runtime, fake), job)
    assert seen and result == {"stale": True}
    with db.session() as s:
        assert not list(s.scalars(select(RoleMemory.id).where(RoleMemory.embedding.is_not(None))))
