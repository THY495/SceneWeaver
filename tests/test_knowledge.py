import pytest
from sqlalchemy import func, select

from chatbot.knowledge import Knowledge, parse_document, tokenize
from chatbot.storage import Chunk, Document, Evidence


def test_chinese_tokenizer_and_document_structure():
    assert "预算" in tokenize("项目预算5000元")
    parts = parse_document("a.md", "# 预算\n项目预算为5000元。\n# 交期\n交期为9月10日。".encode())
    assert parts[0][0] == "预算" and parts[1][0] == "交期"
    with pytest.raises(ValueError):
        parse_document("empty.txt", b"")


async def test_ingest_search_expand_and_isolation(db, config, runtime, fake):
    k = Knowledge(db, config, runtime, fake)
    d, _ = k.upload("local", "项目.md", "# 项目\n灯塔项目预算为5000元，负责人是李明。".encode())
    task = runtime.claim("ingest", background=True)
    await k.ingest(task)
    await k.ingest(task)  # crash after business commit, before checkpoint
    hits = await k.search("local", "灯塔项目预算")
    assert len(hits) == 1 and "5000" in hits[0]["text"]
    assert await k.search("other", "灯塔项目预算") == []
    assert k.expand("other", hits[0]["chunk_id"]) == ""
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Chunk)) == 1


async def test_replacement_and_deletion_exclude_old_version(db, config, runtime, fake):
    k = Knowledge(db, config, runtime, fake)
    d, _ = k.upload("local", "预算.txt", "项目预算3000元。".encode())
    t = runtime.claim("w", background=True)
    await k.ingest(t)
    runtime.finish(t.id, t.lease_version, {})
    k.upload("local", "预算.txt", "项目预算5000元。".encode(), d.id)
    t = runtime.claim("w", background=True)
    await k.ingest(t)
    hits = await k.search("local", "项目预算")
    assert all("3000" not in x["text"] for x in hits)
    assert any("5000" in x["text"] for x in hits)
    with db.session.begin() as s:
        s.get(Document, d.id).deleted = True
    assert await k.search("local", "项目预算") == []


async def test_active_retrieval_preserves_evidence_ids(db, config, runtime, fake):
    k = Knowledge(db, config, runtime, fake)
    k.upload("local", "项目.txt", "灯塔项目预算5000元。".encode())
    t = runtime.claim("i", background=True)
    await k.ingest(t)
    runtime.finish(t.id, t.lease_version, {})
    c = runtime.create_session()
    runtime.submit(c.id, "local", "灯塔项目预算", "q")
    t = runtime.claim("r")
    result = await k.retrieve(t, "灯塔项目预算")
    assert result["evidence"]
    with db.session() as s:
        assert s.get(Evidence, result["evidence"][0]["id"]).owner_id == "local"


async def test_vector_search_real_pg_distance(db, config, runtime, fake):
    k = Knowledge(db, config, runtime, fake)
    k.upload("local", "数据.txt", "与词法无关的内容。".encode())
    t = runtime.claim("w", background=True)
    await k.ingest(t)
    with db.session.begin() as s:
        c = s.scalar(select(Chunk))
        c.embedding = [1.0] + [0.0] * 1023
        c.embedding_model = config.embedding_model

    class StubEmbedding:
        async def encode(self, texts, task_id=None):
            return [[1.0] + [0.0] * 1023 for _ in texts]

    config.embedding_provider = "api"
    k.embedding = StubEmbedding()
    hits = await k.search("local", "不存在的术语")
    assert hits and hits[0]["document"] == "数据.txt"


async def test_hybrid_keeps_exact_project_code_ahead_of_semantic_neighbor(db, config, runtime, fake):
    k = Knowledge(db, config, runtime, fake)
    for name, content in [("a.txt", "ZX7419的批准预算为5000元。"), ("b.txt", "ZX7420的批准预算为6000元。")]:
        k.upload("local", name, content.encode())
        task = runtime.claim("w", background=True)
        await k.ingest(task)
        runtime.finish(task.id, task.lease_version, {})
    with db.session.begin() as s:
        for c in s.scalars(select(Chunk)):
            c.embedding = ([0.8, 0.2] if "7419" in c.content else [1.0, 0.0]) + [0.0] * 1022
            c.embedding_model = config.embedding_model

    class StubEmbedding:
        async def encode(self, texts, task_id=None):
            return [[1.0] + [0.0] * 1023 for _ in texts]

    config.embedding_provider = "api"
    k.embedding = StubEmbedding()
    hits = await k.search("local", "ZX7419预算")
    assert "7419" in hits[0]["text"]
