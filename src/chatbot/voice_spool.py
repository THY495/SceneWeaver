"""Private bounded PCM spools; publication binds one to an approved event.

No route exposes spool IDs. A playback caller must validate its observation,
epoch and voice binding before reading a published spool. Producers only append;
timeouts and crashes must never make partial audio look complete.
"""

import base64
from datetime import timedelta

from sqlalchemy import delete, select, text

from .providers import ProviderError
from .storage import Operation, now, uid

PREFIX = "voice-spool:"
TTL = timedelta(minutes=10)
MAX_BYTES = 24000 * 2 * 30
MAX_SPOOLS = 32


def create(s, task, *, text_value, voice, direction, scope):
    if not text_value or len(text_value) > 80:
        raise ProviderError("语音缓冲仅允许最多80字的首句。")
    s.execute(text("SELECT pg_advisory_xact_lock(731296)"))
    s.execute(delete(Operation).where(Operation.id.startswith(PREFIX), Operation.created_at < now() - TTL))
    existing = s.scalars(select(Operation.id).where(Operation.id.startswith(PREFIX))).all()
    if len(existing) >= MAX_SPOOLS:
        raise ProviderError("私有语音缓冲已满，请稍后再试。")
    key = PREFIX + uid()
    s.add(
        Operation(
            id=key,
            task_id=task.id,
            payload={
                "owner_id": task.owner_id,
                "session_id": task.session_id,
                "epoch": task.epoch,
                "text": text_value,
                "voice": voice,
                "direction": direction,
                "scope": scope,
                "pcm": "",
                "size": 0,
                "status": "producing",
                "event_id": None,
            },
        )
    )
    return key


def locked(s, key, task_id):
    row = s.scalar(select(Operation).where(Operation.id == key).with_for_update())
    if not row or not key.startswith(PREFIX) or row.task_id != task_id or row.created_at < now() - TTL:
        raise ProviderError("私有语音缓冲已失效。")
    return row


def append(s, key, task_id, offset, pcm):
    row = locked(s, key, task_id)
    value = row.payload
    if value["status"] != "producing" or offset != value["size"]:
        raise ProviderError("语音缓冲状态或写入位置已变化。")
    if not pcm or len(pcm) % 2 or offset + len(pcm) > MAX_BYTES:
        raise ProviderError("语音缓冲采样不完整或超过容量。")
    data = base64.b64decode(value["pcm"], validate=True) + pcm
    row.payload = {**value, "pcm": base64.b64encode(data).decode(), "size": len(data)}
    return len(data)


def finish(s, key, task_id, *, successful):
    row = locked(s, key, task_id)
    status = "completed" if successful else "failed"
    if row.payload["status"] != "producing":
        if row.payload["status"] == status:
            return
        raise ProviderError("语音缓冲已终止，不能改写结果。")
    if successful and not row.payload["size"]:
        raise ProviderError("空语音不能标记为完成。")
    row.payload = {**row.payload, "status": status}


def discard(s, key, task_id):
    """Release a rejected/unpublished candidate without deleting public audio."""
    row = locked(s, key, task_id)
    if row.payload["event_id"] is not None:
        raise ProviderError("已发布语音不能作为未审查候选删除。")
    s.delete(row)


def bind(s, key, task, record, *, voice, direction, scope):
    """Only call in the transaction that publishes an approved speech event."""
    from .voice_candidate import first_sentence

    row = locked(s, key, task.id)
    value = row.payload
    if (
        value["owner_id"] != task.owner_id
        or value["session_id"] != task.session_id
        or value["epoch"] != task.epoch
        or record.task_id != task.id
        or value["text"] != first_sentence(record.text, record.payload.get("spoken_parts", []))
        or (value["voice"], value["direction"], value["scope"]) != (voice, direction, scope)
        or not value["size"]
        or value["status"] not in {"producing", "completed"}
        or value["event_id"] not in {None, record.id}
    ):
        raise ProviderError("语音缓冲与已审核台词不匹配。")
    row.payload = {**value, "event_id": record.id}


def read(s, key, playback, *, approved_text, voice_scope, offset):
    """Return new bytes/status only after caller validates live playback rights."""
    row = s.get(Operation, key)
    if not row or not key.startswith(PREFIX) or row.created_at < now() - TTL:
        raise ProviderError("语音缓冲已过期。")
    value = row.payload
    if (
        playback.chunk_index != 0
        or value["event_id"] != playback.event_id
        or value["event_id"] is None
        or value["session_id"] != playback.session_id
        or value["epoch"] != playback.epoch
        or value["text"] != approved_text
        or value["voice"] != playback.voice
        or value["direction"] != playback.timings.get("voice_direction", "")
        or value["scope"] != voice_scope
        or offset < 0
        or offset % 2
        or offset > value["size"]
    ):
        raise ProviderError("语音缓冲不属于当前播放。")
    data = base64.b64decode(value["pcm"], validate=True)
    if len(data) != value["size"] or len(data) > MAX_BYTES or len(data) % 2:
        raise ProviderError("语音缓冲内容损坏。")
    return data[offset:], value["status"]
