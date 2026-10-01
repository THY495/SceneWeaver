"""Short-lived, bounded database cache shared by worker and playback process."""

import base64
import binascii
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, text

from .providers import ProviderError
from .storage import Operation, now
from .tts_adapters import binding_scope, selected_model
from .voice import digest
from .voice_candidate import first_sentence

PREFIX = "voice-ready:"
TTL = timedelta(minutes=10)
MAX_BYTES = 24000 * 2 * 30


@dataclass(frozen=True)
class SpoolReference:
    key: str


def scope(config):
    return (
        binding_scope(config)
        if config.tts_provider != "qwen_legacy"
        else digest(config.tts_key() + selected_model(config) + config.tts_transport + config.tts_api_url)
    )


def bind(s, task, record, audio):
    """Call only inside the approved world publication transaction."""
    expected = first_sentence(record.text, record.payload.get("spoken_parts", []))
    if audio.text != expected or not audio.pcm or len(audio.pcm) % 2 or len(audio.pcm) > MAX_BYTES:
        raise ProviderError("预生成音频与正式台词不符。")
    if audio.spool_id:
        from . import voice_spool

        voice_spool.bind(
            s,
            audio.spool_id,
            task,
            record,
            voice=audio.voice,
            direction=audio.direction,
            scope=audio.scope,
        )
    s.execute(text("SELECT pg_advisory_xact_lock(731295)"))
    s.execute(delete(Operation).where(Operation.id.startswith(PREFIX), Operation.created_at < now() - TTL))
    excess = list(
        s.scalars(
            select(Operation.id)
            .where(Operation.id.startswith(PREFIX))
            .order_by(Operation.created_at.desc())
            .offset(31)
        )
    )
    if excess:
        s.execute(delete(Operation).where(Operation.id.in_(excess)))
    s.add(
        Operation(
            id=PREFIX + record.id,
            task_id=task.id,
            payload={
                "owner_id": task.owner_id,
                "session_id": task.session_id,
                "epoch": task.epoch,
                "text": audio.text,
                "voice": audio.voice,
                "direction": audio.direction,
                "scope": audio.scope,
                "pcm": base64.b64encode(audio.pcm).decode() if not audio.spool_id else "",
                "spool_id": audio.spool_id,
            },
        )
    )
    record.payload = {**record.payload, "voice_first_chunk": audio.text}


def read(s, playback, config, approved_text):
    """Caller must first revalidate the playback's observation and epoch."""
    if playback.chunk_index != 0:
        return None
    row = s.get(Operation, PREFIX + playback.event_id)
    if not row or row.created_at < now() - TTL:
        return None
    value = row.payload
    if (
        value["session_id"] != playback.session_id
        or value["epoch"] != playback.epoch
        or value["text"] != approved_text
        or value["voice"] != playback.voice
        or value["direction"] != playback.timings.get("voice_direction", "")
        or value["scope"] != scope(config)
    ):
        return None
    if value.get("spool_id"):
        from . import voice_spool

        key = value["spool_id"]
        # Before any samples are sent, an expired or failed spool may be freshly
        # synthesized. Once streaming starts, a failed tail must fail the stream.
        try:
            data, status = voice_spool.read(
                s, key, playback, approved_text=approved_text, voice_scope=scope(config), offset=0
            )
        except ProviderError:
            return None
        return SpoolReference(key) if data and status in {"producing", "completed"} else None
    encoded = value.get("pcm", "")
    if not isinstance(encoded, str) or len(encoded) > MAX_BYTES * 4 // 3 + 4:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    return data if data and len(data) % 2 == 0 and len(data) <= MAX_BYTES else None
