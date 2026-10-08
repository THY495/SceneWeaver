"""Observe logic/voice boundaries without changing leases or publishing private audio."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient
from support.story import launch

from chatbot import voice_spool
from chatbot.api import create_app
from chatbot.npc.context import NPCServices
from chatbot.npc.turns import respond_player
from chatbot.observability.timing import TimingTrace
from chatbot.storage import Operation, Task
from chatbot.voice import VoiceService


async def test_logic_returns_before_published_tail_but_task_lease_stays_alive(
    db, config, runtime, fake, monkeypatch
):
    from chatbot.npc import directed_scene, director

    config.product = "roleplay"
    config.tts_enabled = True
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    runtime.submit(player, config.workspace_owner, "请大家商量。", "finalize-timing", acknowledgement=True)
    task = runtime.claim("foreground")
    voice = object.__new__(VoiceService)
    voice.db, voice.runtime = db, runtime
    voice.config = SimpleNamespace(
        tts_provider="qwen_legacy",
        tts_transport="http",
        tts_model="test",
        tts_api_url="test",
        tts_key=lambda: "fake",
        tts_usd_per_million_chars=20,
    )
    voice.available = lambda: True
    voice.billing = SimpleNamespace(reserve=Mock(return_value="usage"), settle=Mock(), close=AsyncMock())
    voice.realtime = SimpleNamespace(close=AsyncMock())
    voice.ack_realtime = SimpleNamespace(close=AsyncMock())
    voice.http = SimpleNamespace(aclose=AsyncMock())
    tail, returned = asyncio.Event(), asyncio.Event()

    async def source(*args):
        yield b"\0\1" * 2400
        await tail.wait()
        yield b"\0\2" * 100

    voice.pcm = source
    monkeypatch.setattr("chatbot.voice.VoiceService", lambda *args: voice)
    monkeypatch.setattr(director, "snapshot", lambda *args: SimpleNamespace())

    async def logic(services, current_task, *args):
        audio = await services.voice.stream_candidate(current_task, "你好。", "Cherry")
        with db.session.begin() as session:
            voice_spool.bind(
                session,
                audio.spool_id,
                current_task,
                SimpleNamespace(
                    id="event", task_id=task.id, text="你好。", payload={"spoken_parts": ["你好。"]}
                ),
                voice=audio.voice,
                direction=audio.direction,
                scope=audio.scope,
            )
        returned.set()
        return {"answer": "你好。"}

    monkeypatch.setattr(directed_scene, "respond_directed", logic)
    trace = TimingTrace(task)
    with trace.activate():
        job = asyncio.create_task(respond_player(NPCServices(db, config, runtime, fake, None, None), task))
        try:
            await asyncio.wait_for(returned.wait(), 2)
            await asyncio.sleep(0)
            spans = {span["name"]: span for span in trace.spans}
            assert spans["npc.directed_logic"]["status"] == "ok"
            assert "returned" in spans["npc.directed_logic"]["marks"]
            assert spans["voice.published_drain"]["status"] == "running"
            assert spans["voice.published_drain"]["attributes"] == {
                "published_producer_count": 1,
                "pending_published_producer_count": 1,
            }
            assert not job.done()
            runtime.check(task.id, task.lease_version)
            with db.session() as session:
                assert session.get(Task, task.id).status == "running"
        finally:
            tail.set()
            result = await asyncio.wait_for(job, 2)
    assert result == {"answer": "你好。"}
    spans = {span["name"]: span for span in trace.spans}
    links = {
        "npc.directed_logic": "npc.turn",
        "voice.turn_finalize": "npc.turn",
        "voice.service_finalize": "voice.turn_finalize",
        "voice.producer_finalize": "voice.service_finalize",
        "voice.published_drain": "voice.producer_finalize",
        "voice.producer_cleanup": "voice.producer_finalize",
        "voice.resources_close": "voice.service_finalize",
    }
    for child, parent in links.items():
        assert spans[child]["status"] == "ok"
        assert spans[child]["parent_id"] == spans[parent]["id"]
    assert spans["voice.turn_finalize"]["start_ms"] >= (
        spans["npc.directed_logic"]["start_ms"] + spans["npc.directed_logic"]["duration_ms"]
    )
    assert "你好" not in str(trace.spans)
    with db.session() as session:
        key = voice._candidate_producers.entries
        assert key == []
        rows = list(session.query(Operation).filter(Operation.id.startswith("voice-spool:")).all())
        assert len(rows) == 1 and rows[0].payload["status"] == "completed"
        assert rows[0].payload["size"] == 5000
    voice.billing.settle.assert_called_once()
    assert voice.billing.settle.call_args.args[-1] == "completed"
