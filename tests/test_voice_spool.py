from datetime import timedelta
from types import SimpleNamespace

import pytest

from chatbot import voice_spool as spool
from chatbot.providers import ProviderError
from chatbot.storage import Operation, now


def identities():
    task = SimpleNamespace(id="task", owner_id="owner", session_id="player", epoch=3)
    record = SimpleNamespace(id="event", task_id="task", text="你好。", payload={"spoken_parts": ["你好。"]})
    playback = SimpleNamespace(
        event_id="event", session_id="player", epoch=3, voice="voice", timings={}, chunk_index=0
    )
    return task, record, playback


def create(db):
    task, _, _ = identities()
    with db.session.begin() as s:
        return spool.create(s, task, text_value="你好。", voice="voice", direction="", scope="scope")


def read(db, key, playback, offset=0):
    with db.session() as s:
        return spool.read(s, key, playback, approved_text="你好。", voice_scope="scope", offset=offset)


def test_private_prefix_cannot_play_until_binding_commits(db):
    task, record, playback = identities()
    key = create(db)
    with db.session.begin() as s:
        spool.append(s, key, task.id, 0, b"\x01\x00")
    with pytest.raises(ProviderError, match="不属于"):
        read(db, key, playback)
    with pytest.raises(RuntimeError):
        with db.session.begin() as s:
            spool.bind(s, key, task, record, voice="voice", direction="", scope="scope")
            raise RuntimeError("publication rolled back")
    with pytest.raises(ProviderError, match="不属于"):
        read(db, key, playback)
    with db.session.begin() as s:
        spool.bind(s, key, task, record, voice="voice", direction="", scope="scope")
    assert read(db, key, playback) == (b"\x01\x00", "producing")
    with db.session.begin() as s:
        spool.append(s, key, task.id, 2, b"\x02\x00")
        spool.finish(s, key, task.id, successful=True)
    assert read(db, key, playback, 2) == (b"\x02\x00", "completed")
    assert read(db, key, playback, 4) == (b"", "completed")
    with db.session.begin() as s, pytest.raises(ProviderError, match="已发布"):
        spool.discard(s, key, task.id)
    playback.chunk_index = 1
    with pytest.raises(ProviderError, match="不属于"):
        read(db, key, playback)
    playback.chunk_index = 0
    playback.epoch += 1
    with pytest.raises(ProviderError, match="不属于"):
        read(db, key, playback)


def test_failed_tail_cannot_be_relabelled_complete_or_appended(db):
    task, record, playback = identities()
    key = create(db)
    with db.session.begin() as s:
        spool.append(s, key, task.id, 0, b"\x00\x00")
        spool.bind(s, key, task, record, voice="voice", direction="", scope="scope")
        spool.finish(s, key, task.id, successful=False)
    assert read(db, key, playback)[1] == "failed"
    with db.session.begin() as s, pytest.raises(ProviderError, match="终止"):
        spool.finish(s, key, task.id, successful=True)
    with db.session.begin() as s, pytest.raises(ProviderError, match="状态"):
        spool.append(s, key, task.id, 2, b"\x00\x00")


@pytest.mark.parametrize(
    "offset,pcm",
    [(2, b"\0\0"), (0, b"\0"), (0, b"\0" * (spool.MAX_BYTES + 2))],
    ids=["out-of-order", "partial-sample", "oversize"],
)
def test_reject_out_of_order_partial_sample_and_oversize(db, offset, pcm):
    key = create(db)
    with db.session.begin() as s, pytest.raises(ProviderError):
        spool.append(s, key, "task", offset, pcm)
    with db.session() as s:
        assert s.get(Operation, key).payload["size"] == 0


def test_wrong_text_and_producer_and_expiry_fail_closed(db):
    task, record, playback = identities()
    key = create(db)
    with db.session.begin() as s, pytest.raises(ProviderError):
        spool.append(s, key, "other-task", 0, b"\0\0")
    with db.session.begin() as s:
        spool.append(s, key, task.id, 0, b"\0\0")
    record.text = "替换台词。"
    record.payload = {"spoken_parts": [record.text]}
    with db.session.begin() as s, pytest.raises(ProviderError, match="不匹配"):
        spool.bind(s, key, task, record, voice="voice", direction="", scope="scope")
    with db.session.begin() as s:
        s.get(Operation, key).created_at = now() - spool.TTL - timedelta(seconds=1)
    with pytest.raises(ProviderError, match="过期"):
        read(db, key, playback)


def test_capacity_rejects_without_evicting_live_stream(db, monkeypatch):
    monkeypatch.setattr(spool, "MAX_SPOOLS", 1)
    key = create(db)
    with pytest.raises(ProviderError, match="已满"):
        create(db)
    with db.session() as s:
        assert s.get(Operation, key) is not None
    with db.session.begin() as s:
        spool.discard(s, key, "task")
    assert create(db) != key
