import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from chatbot.providers import ProviderError
from chatbot.voice import VoiceService
from chatbot.voice_candidate import aligned_performance, first_sentence


async def test_service_shutdown_attempts_every_cleanup_even_if_pool_close_fails():
    voice = object.__new__(VoiceService)
    voice._candidate_producers = SimpleNamespace(close=AsyncMock())
    voice._tts_connections = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("close failed")))
    voice.realtime = SimpleNamespace(close=AsyncMock())
    voice.ack_realtime = SimpleNamespace(close=AsyncMock())
    voice.http = SimpleNamespace(aclose=AsyncMock())
    voice.billing = SimpleNamespace(close=AsyncMock())
    with pytest.raises(RuntimeError, match="close failed"):
        await voice.close()
    for resource in [
        voice._candidate_producers,
        voice._tts_connections,
        voice.realtime,
        voice.ack_realtime,
        voice.billing,
    ]:
        resource.close.assert_awaited_once()
    voice.http.aclose.assert_awaited_once()


def test_first_sentence_is_exact_bounded_spoken_projection():
    assert first_sentence("（点头）你好！接着说。", ["你好！接着说。"]) == "你好！"
    assert first_sentence("长" * 100, ["长" * 100]) == "长" * 80
    assert first_sentence("只有动作", []) == ""
    with pytest.raises(ProviderError):
        first_sentence("实际台词", ["编造的台词"])


def test_misaligned_performance_is_removed_before_speculative_voice_direction_selection():
    spoken = ["你好。", "请坐。"]
    performance = [{"text": "你好。请坐。", "voice_direction": "温柔"}]
    assert aligned_performance(spoken, performance) == []
    assert aligned_performance([], performance) == []
    aligned = [{"text": part, "voice_direction": "平静"} for part in spoken]
    assert aligned_performance(spoken, aligned) == aligned


def service():
    result = object.__new__(VoiceService)
    result.available = lambda: True
    result.config = SimpleNamespace(
        tts_provider="qwen_legacy",
        tts_transport="http",
        tts_model="test",
        tts_api_url="test",
        tts_key=lambda: "fake",
        tts_usd_per_million_chars=20,
    )
    result.runtime = SimpleNamespace(check=Mock())
    result.billing = SimpleNamespace(reserve=Mock(return_value="usage"), settle=Mock())
    return result


async def test_private_audio_uses_budget_and_returns_no_playback_identifier():
    voice = service()

    async def source(*args):
        yield b"\x00"
        yield b"\x00\x00\x00"

    voice.pcm = source
    value = await voice.buffer_candidate(SimpleNamespace(id="t", lease_version=1), "你好。", "Cherry")
    assert value.pcm == b"\x00" * 4 and value.text == "你好。"
    assert not hasattr(value, "playback_id")
    voice.billing.reserve.assert_called_once()
    assert voice.billing.settle.call_args.args[-1] == "completed"


async def test_cancel_closes_source_and_settles_submitted_audio():
    voice = service()
    started, closed = asyncio.Event(), asyncio.Event()

    async def source(*args):
        try:
            started.set()
            await asyncio.Event().wait()
            yield b""
        finally:
            closed.set()

    voice.pcm = source
    job = asyncio.create_task(
        voice.buffer_candidate(SimpleNamespace(id="t", lease_version=1), "你好。", "Cherry")
    )
    await started.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert closed.is_set()
    assert voice.billing.settle.call_args.args[-1] == "interrupted"


async def test_published_producer_tail_failure_is_preserved_for_reader(db):
    from chatbot import voice_spool
    from chatbot.observability.timing import TimingTrace
    from chatbot.storage import Operation, now

    voice = service()
    voice.db = db
    task = SimpleNamespace(
        id="t",
        lease_version=1,
        owner_id="o",
        session_id="p",
        epoch=1,
        attempt=1,
        created_at=now(),
        updated_at=now(),
    )
    fail = asyncio.Event()

    async def source(*args):
        yield b"\0\1" * 2400
        await fail.wait()
        raise ProviderError("tail connection lost: private credential/provider payload")

    voice.pcm = source
    trace = TimingTrace(task)
    with trace.activate():
        audio = await voice.stream_candidate(task, "你好。", "Cherry")
        with db.session.begin() as s:
            voice_spool.bind(
                s,
                audio.spool_id,
                task,
                SimpleNamespace(id="event", task_id="t", text="你好。", payload={"spoken_parts": ["你好。"]}),
                voice=audio.voice,
                direction=audio.direction,
                scope=audio.scope,
            )
        fail.set()
        await voice._candidate_producers.close()
    with db.session() as s:
        assert s.get(Operation, audio.spool_id).payload["status"] == "failed"
    voice.billing.settle.assert_called_once()
    assert voice.billing.settle.call_args.args[-1] == "failed"
    span = next(span for span in trace.spans if span["name"] == "voice.candidate_stream")
    assert span["attributes"]["audio_status"] == "failed"
    assert span["attributes"]["audio_error_type"] == "ProviderError"
    assert "private credential" not in str(trace.spans)
    assert "你好" not in str(trace.spans)


async def test_candidate_close_diagnostics_reach_trace_without_provider_reason(db):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    from chatbot.observability.timing import TimingTrace
    from chatbot.storage import now

    voice = service()
    voice.db = db

    async def source(*args):
        yield b"\0\1" * 30
        raise ConnectionClosedError(None, Close(1011, "private reason"))

    voice.pcm = source
    task = SimpleNamespace(
        id="t",
        lease_version=1,
        attempt=1,
        owner_id="o",
        session_id="p",
        epoch=1,
        created_at=now(),
        updated_at=now(),
    )
    trace = TimingTrace(task)
    with trace.activate():
        with pytest.raises(ConnectionClosedError):
            await voice.stream_candidate(task, "你好。", "Cherry")
    span = next(s for s in trace.spans if s["name"] == "voice.candidate_stream")
    assert span["attributes"]["audio_sent_close_code"] == 1011
    assert span["attributes"]["audio_failure_phase"] == "receive_audio"
    assert "audio_received_close_code" not in span["attributes"]
    assert "private reason" not in str(trace.spans)
    assert voice.billing.settle.call_args.args[-1] == "failed"


@pytest.mark.parametrize(
    "raw", [b"", b"\x00", b"\x00" * (24000 * 2 * 30 + 2)], ids=["empty", "odd", "oversize"]
)
async def test_invalid_or_excess_audio_is_not_returned(raw):
    voice = service()

    async def source(*args):
        yield raw

    voice.pcm = source
    with pytest.raises(ProviderError):
        await voice.buffer_candidate(SimpleNamespace(id="t", lease_version=1), "你好。", "Cherry")
    assert voice.billing.settle.call_args.args[-1] == "failed"


async def test_stream_candidate_ready_before_tail_and_close_drains_published_audio(db):
    from chatbot import voice_spool
    from chatbot.storage import Operation

    voice = service()
    voice.db = db
    task = SimpleNamespace(id="t", lease_version=1, owner_id="o", session_id="p", epoch=1)
    tail = asyncio.Event()

    async def source(*args):
        yield b"\0\1" * 2400
        await tail.wait()
        yield b"\0\2" * 100

    voice.pcm = source
    audio = await asyncio.wait_for(voice.stream_candidate(task, "你好。", "Cherry"), 2)
    with db.session.begin() as s:
        assert s.get(Operation, audio.spool_id).payload["status"] == "producing"
        voice_spool.bind(
            s,
            audio.spool_id,
            task,
            SimpleNamespace(id="event", task_id="t", text="你好。", payload={"spoken_parts": ["你好。"]}),
            voice=audio.voice,
            direction=audio.direction,
            scope=audio.scope,
        )
    drain = asyncio.create_task(voice._candidate_producers.close())
    await asyncio.sleep(0)
    assert not drain.done()
    tail.set()
    await asyncio.wait_for(drain, 2)
    with db.session() as s:
        row = s.get(Operation, audio.spool_id)
        assert row.payload["status"] == "completed" and row.payload["size"] == 5000
    voice.billing.settle.assert_called_once()
    assert voice.billing.settle.call_args.args[-1] == "completed"


@pytest.mark.parametrize("reason", ["rejected", "unpublished", "cancel_before_ready"])
async def test_speculative_producer_is_cancelled_and_discarded(db, reason):
    from sqlalchemy import select

    from chatbot.performance_candidate import CandidateIdentity, prepare_candidate
    from chatbot.storage import Operation

    voice = service()
    voice.db = db
    task = SimpleNamespace(id="t", lease_version=1, owner_id="o", session_id="p", epoch=1)
    began, closed = asyncio.Event(), asyncio.Event()

    async def source(*args):
        try:
            if reason != "cancel_before_ready":
                yield b"\0\1" * 2400
            began.set()
            await asyncio.Event().wait()
        finally:
            closed.set()

    voice.pcm = source
    if reason == "rejected":

        async def review():
            await began.wait()
            return False

        result = await prepare_candidate(
            CandidateIdentity("p", 1, "plan", 1, "job", 1, "hash", "voice"),
            review,
            lambda: voice.stream_candidate(task, "你好。", "Cherry"),
            lambda: True,
            discard_audio=voice.discard_candidate,
        )
        assert result.status == "rejected"
    elif reason == "unpublished":
        await voice.stream_candidate(task, "你好。", "Cherry")
    else:
        pending = asyncio.create_task(voice.stream_candidate(task, "你好。", "Cherry"))
        await began.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    await voice._candidate_producers.close()
    assert closed.is_set()
    with db.session() as s:
        assert not list(s.scalars(select(Operation).where(Operation.id.startswith("voice-spool:"))))
    voice.billing.settle.assert_called_once()
    assert voice.billing.settle.call_args.args[-1] == "interrupted"
