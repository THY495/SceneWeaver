import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch
from support.voice import app as app

from chatbot.api import create_app
from chatbot.providers import ProviderError
from chatbot.storage import Conversation, Task, Usage, VoiceDelivery
from chatbot.voice import validate_spoken_parts


def test_preview_requires_tts_key_and_does_not_create_story_events(app, db, config):
    from pydantic import SecretStr
    from sqlalchemy import func

    from chatbot.storage import SceneRecord

    with TestClient(app) as client:
        payload = {"voice": "Cherry", "text": "你好。", "direction": ""}
        config.tts_api_key = SecretStr("")
        assert client.post("/api/voice/preview", json=payload).status_code == 409
        config.tts_api_key = SecretStr("test")
        response = client.post("/api/voice/preview", json=payload)
        assert response.status_code == 200, response.text
        assert base64.b64decode(response.json()["audio"]).startswith(b"RIFF")
        with db.session() as s:
            assert s.scalar(select(func.count()).select_from(SceneRecord)) == 0
            assert s.scalar(select(Usage).where(Usage.role == "tts")).cost > 0
        assert client.post("/api/voice/preview", json={**payload, "direction": "轻声"}).status_code == 400


def test_fish_binding_and_byte_budget_through_player_api(app, runtime, db, config):
    from pydantic import SecretStr

    config.tts_provider = "fish"
    config.tts_adapter_model = "s2.1-pro"
    config.tts_adapter_api_key = SecretStr("fish-test-only")
    config.tts_voices = {"voice-test": "测试音色"}
    with TestClient(app) as client:
        _, actors, player, task, eid = setup(client, runtime, db, config)
        assert (
            client.put(f"/api/voice/actors/{actors[0]}/profile", json={"voice": "voice-test"}).status_code
            == 200
        )
        response = prepare(client, player, task, eid)
        assert response.status_code == 200, response.text
        state = client.get(f"/api/voice/playbacks/{response.json()['id']}").json()
        assert state["timings"]["billing_quote"]["unit"] == "utf8_bytes"
        config.tts_adapter_api_key = SecretStr("different-account")
        assert client.post(f"/api/voice/playbacks/{response.json()['id']}/audio").status_code == 409
        client.post(
            f"/api/voice/playbacks/{response.json()['id']}/receipt",
            json={"seq": 1, "status": "interrupted", "played_samples": 0},
        )
        assert prepare(client, player, task, eid, request_id="changed").status_code == 400


def test_authored_opening_voice_is_observed_scoped_and_epoch_bound(app, runtime, db, config):
    from chatbot.scene_execution import commit_block
    from chatbot.storage import Story

    with TestClient(app) as client:
        story, actors, player = launch(client)
        with db.session.begin() as s:
            record = commit_block(
                s,
                s.get(Story, story),
                {"id": "dock", "observers": ["player"], "opening": [{}]},
                {"id": "greeting", "speaker": "keeper", "text": "（挥手）你好。"},
                {"keeper": actors[0], "player": player},
            )
            eid = record.id
        result = client.get(f"/api/voice/sessions/{player}/utterances").json()
        assert result["utterances"][0]["chunks"] == ["你好。"]
        assert result["utterances"][0]["task_id"] == ""
        body = {"request_id": "opening", "event_id": eid, "chunk_index": 0, "epoch": result["epoch"]}
        response = client.post(f"/api/voice/sessions/{player}/playbacks", json=body)
        assert response.status_code == 200, response.text
        pid = response.json()["id"]
        assert len(client.post(f"/api/voice/playbacks/{pid}/audio").content) == 4800
        client.post(
            f"/api/voice/playbacks/{pid}/receipt",
            json={"seq": 1, "played_samples": 0, "status": "interrupted"},
        )
        runtime.submit(player, config.workspace_owner, "继续", "next")
        epoch = client.get(f"/api/sessions/{player}").json()["epoch"]
        body.update(request_id="old-opening", epoch=epoch)
        assert client.post(f"/api/voice/sessions/{player}/playbacks", json=body).status_code == 409
        body.update(request_id="replay-opening", replay=True)
        assert client.post(f"/api/voice/sessions/{player}/playbacks", json=body).status_code == 200
        _, _, outsider = launch(client)
        assert client.post(f"/api/voice/sessions/{outsider}/playbacks", json=body).status_code == 404


def setup(client, runtime, db, config):
    story, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "打个招呼", "voice-turn")
    task = runtime.claim("test-voice")
    with db.session() as s:
        revision = s.get(Conversation, actors[0]).revision
    eid = runtime.commit_role_segment(
        task,
        actors[0],
        "（点头）你好，今晚我们先聊聊。",
        step=0,
        revision=revision,
        intent={"act": "speak", "addressee_id": player, "wait_for_player": True},
        spoken_parts=["你好，今晚我们先聊聊。"],
    )
    return story, actors, player, task, eid


def prepare(client, player, task, eid, **kwargs):
    return client.post(
        f"/api/voice/sessions/{player}/playbacks",
        json={"request_id": "play", "event_id": eid, "chunk_index": 0, "epoch": task.epoch, **kwargs},
    )


def test_generated_speech_delivery_has_own_call_allowance(app, runtime, db, config):
    with TestClient(app) as client:
        _, _, player, task, eid = setup(client, runtime, db, config)
        with db.session.begin() as s:
            s.get(Task, task.id).calls = config.max_calls_per_task
        response = prepare(client, player, task, eid)
        assert response.status_code == 200, response.text
        playback = response.json()["id"]
        streamed = client.post(f"/api/voice/playbacks/{playback}/audio")
        assert streamed.status_code == 200 and streamed.content
        with db.session() as s:
            delivery = s.scalar(select(Task).where(Task.dedupe_key == f"voice-delivery:{player}:{eid}"))
            assert delivery is not None and delivery.parent_id is None
            assert delivery.calls == 1
            assert s.get(Task, task.id).calls == config.max_calls_per_task
            assert s.scalar(select(Usage).where(Usage.task_id == delivery.id, Usage.role == "tts"))


def test_audio_start_returns_scoped_release_only_after_pcm_and_is_idempotent(app, runtime, db, config):
    from chatbot.storage import Observation, Operation, SceneRecord, Story

    with TestClient(app) as client:
        story, _, player, task, eid = setup(client, runtime, db, config)
        with db.session.begin() as s:
            record = s.get(SceneRecord, eid)
            record.payload = {**record.payload, "presentation": {"version": 1, "audio_status": "ready"}}
            s.get(Observation, (player, eid)).perception = {
                "text": "今晚我们先聊聊。",
                "speaker_known": False,
            }
            before = s.get(Story, story).seq
        snapshot_url = f"/api/role/players/{player}/snapshot"
        before_snapshot = client.get(snapshot_url)
        pending_event = next(e for e in before_snapshot.json()["events"] if e["id"] == eid)
        assert pending_event["presentation_pending"] and pending_event["text"] == ""
        assert pending_event["actor_id"] is None
        assert "今晚我们先聊聊" not in before_snapshot.text
        assert "今晚我们先聊聊" not in client.get(f"/api/sessions/{player}").text
        utterance = client.get(f"/api/voice/sessions/{player}/utterances").json()["utterances"][0]
        assert utterance["presentation_pending"] and utterance["chunks"] == [""]
        text_event = next(
            e for e in client.get(snapshot_url + "?audio=false").json()["events"] if e["id"] == eid
        )
        assert text_event["text"] == "今晚我们先聊聊。"
        pid = prepare(client, player, task, eid).json()["id"]
        endpoint = f"/api/voice/playbacks/{pid}/start"
        assert client.post(endpoint, json={}).status_code == 409
        with db.session() as s:
            assert not list(s.scalars(select(Operation).where(Operation.id.startswith("player-release:"))))
        assert client.post(f"/api/voice/playbacks/{pid}/audio").status_code == 200
        response = client.post(endpoint, json={})
        assert response.status_code == 200, response.text
        release = response.json()["release"]
        assert release["release_id"] == eid
        assert release["playback_id"] == pid
        assert release["reason"] == "audio_ready"
        assert release["event"]["actor_id"] is None
        assert release["event"]["text"] == "今晚我们先聊聊。"
        assert "你好" not in response.text
        assert client.post(endpoint, json={}).json()["release"] == release
        released_event = next(e for e in client.get(snapshot_url).json()["events"] if e["id"] == eid)
        assert released_event["text"] == "今晚我们先聊聊。"
        assert "presentation_pending" not in released_event
        with db.session() as s:
            assert s.get(Story, story).seq == before
            rows = list(s.scalars(select(Operation).where(Operation.id.startswith("player-release:"))))
            assert len(rows) == 1
            assert "event" not in rows[0].payload  # Never persist a second copy of the speech.
        runtime.submit(player, config.workspace_owner, "等一下", "release-new-epoch")
        assert client.post(endpoint, json={}).status_code == 409


@pytest.mark.parametrize("partial", [False, True])
def test_text_fallback_blocks_auto_retry_but_allows_scoped_manual_replay(app, runtime, db, config, partial):
    from sqlalchemy import func

    from chatbot.storage import Observation, SceneRecord, VoicePlayback

    with TestClient(app) as client:
        _, _, player, task, eid = setup(client, runtime, db, config)
        with db.session.begin() as s:
            record = s.get(SceneRecord, eid)
            record.payload = {
                **record.payload,
                "presentation": {"version": 1, "mode": "text_fallback", "audio_status": "timeout"},
            }
            if partial:
                observation = s.get(Observation, (player, eid))
                observation.perception = {"text": "今晚我们先聊聊。", "speaker_known": False}
        row = client.get(f"/api/voice/sessions/{player}/utterances").json()["utterances"][0]
        assert row["text_fallback"]
        if partial:
            assert row["actor_id"] is None
            assert row["chunks"] == ["今晚我们先聊聊。"]
        rejected = prepare(client, player, task, eid)
        assert rejected.status_code == 409
        with db.session() as s:
            assert s.scalar(select(func.count()).select_from(VoicePlayback)) == 0
            assert s.scalar(select(func.count()).select_from(Usage)) == 0
        replay = prepare(client, player, task, eid, replay=True)
        assert replay.status_code == 200, replay.text
        audio = client.post(f"/api/voice/playbacks/{replay.json()['id']}/audio")
        assert audio.status_code == 200


@pytest.mark.parametrize("cache_state", ["valid", "expired", "account_changed", "partial"])
def test_committed_audio_cache_reuses_pcm_without_second_charge(app, runtime, db, config, cache_state):
    from datetime import timedelta

    from chatbot.storage import Operation, SceneRecord, now
    from chatbot.voice import actor_voice
    from chatbot.voice_cache import bind, scope
    from chatbot.voice_candidate import CandidateAudio, first_sentence

    with TestClient(app) as client:
        _, actors, player, task, eid = setup(client, runtime, db, config)
        with db.session.begin() as s:
            record = s.get(SceneRecord, eid)
            voice = actor_voice(s, s.get(Conversation, actors[0]), config)
            bind(
                s,
                task,
                record,
                CandidateAudio(
                    first_sentence(record.text, record.payload["spoken_parts"]),
                    voice,
                    "",
                    scope(config),
                    b"\x02\x03" * 100,
                ),
            )
        if cache_state == "expired":
            with db.session.begin() as s:
                s.get(Operation, "voice-ready:" + eid).created_at = now() - timedelta(minutes=11)
        elif cache_state == "account_changed":
            from pydantic import SecretStr

            config.tts_api_key = SecretStr("different-test-account")
        elif cache_state == "partial":
            from chatbot.storage import Observation

            with db.session.begin() as s:
                observed = s.scalar(
                    select(Observation).where(
                        Observation.event_id == eid,
                        Observation.actor_id == player,
                    )
                )
                observed.perception = {"text": "今晚我们先聊聊。", "speaker_known": True}
            visible = client.get(f"/api/voice/sessions/{player}/utterances").json()["utterances"]
            assert visible[0]["chunks"] == ["今晚我们先聊聊。"]
        cache_valid = cache_state == "valid"
        pid = prepare(client, player, task, eid).json()["id"]
        audio = client.post(f"/api/voice/playbacks/{pid}/audio")
        assert audio.status_code == 200
        assert audio.content == (b"\x02\x03" * 100 if cache_valid else b"\x00\x01" * 2400)
        with db.session() as s:
            charges = list(s.scalars(select(Usage).where(Usage.role == "tts")))
            assert len(charges) == (0 if cache_valid else 1)


@pytest.mark.parametrize("tail", ["completed", "failed", "new_epoch"])
async def test_published_spool_streams_before_tail_and_never_completes_truncation(
    app, runtime, db, config, tail
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from chatbot import voice_spool
    from chatbot.storage import SceneRecord, VoicePlayback
    from chatbot.voice import actor_voice
    from chatbot.voice_cache import bind, scope
    from chatbot.voice_candidate import CandidateAudio, first_sentence

    with TestClient(app) as client:
        _, actors, player, task, eid = setup(client, runtime, db, config)
        with db.session.begin() as s:
            record = s.get(SceneRecord, eid)
            voice = actor_voice(s, s.get(Conversation, actors[0]), config)
            text = first_sentence(record.text, record.payload["spoken_parts"])
            key = voice_spool.create(s, task, text_value=text, voice=voice, direction="", scope=scope(config))
            s.flush()
            voice_spool.append(s, key, task.id, 0, b"\x02\x03" * 100)
            bind(s, task, record, CandidateAudio(text, voice, "", scope(config), b"\x02\x03" * 100, key))
        pid = prepare(client, player, task, eid).json()["id"]
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
        stream = app.state.voice.stream(pid, request)
        assert await anext(stream) == b"\x02\x03" * 100
        with db.session() as s:
            assert not s.get(VoicePlayback, pid).stream_done
            assert s.get(VoicePlayback, pid).timings["approved_audio_streaming"] is True
        if tail == "new_epoch":
            runtime.submit(player, config.workspace_owner, "等一下", "spool-interrupt")
        else:
            with db.session.begin() as s:
                voice_spool.append(s, key, task.id, 200, b"\x04\x05" * 100)
                voice_spool.finish(s, key, task.id, successful=tail == "completed")
        remaining = [packet async for packet in stream]
        assert remaining == ([b"\x04\x05" * 100] if tail == "completed" else [])
        with db.session() as s:
            state = s.get(VoicePlayback, pid)
            assert state.stream_done is (tail == "completed")
            assert not list(s.scalars(select(Usage).where(Usage.role == "tts")))


def test_stream_receipts_idempotency_and_independent_caption_delivery(app, runtime, db, config):
    with TestClient(app) as client:
        story, _, player, task, eid = setup(client, runtime, db, config)
        r = prepare(client, player, task, eid)
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        assert prepare(client, player, task, eid).json()["id"] == pid
        receipt = f"/api/voice/playbacks/{pid}/receipt"
        assert (
            client.post(receipt, json={"seq": 1, "played_samples": 0, "status": "completed"}).status_code
            == 409
        )
        assert client.post(f"/api/voice/sessions/{player}/utterances/{eid}/visible").status_code == 200
        audio = client.post(f"/api/voice/playbacks/{pid}/audio")
        assert len(audio.content) == 4800 and audio.headers["x-audio-format"] == "pcm_s16le"
        assert client.post(f"/api/voice/playbacks/{pid}/audio").status_code == 409
        assert client.get(f"/api/voice/playbacks/{pid}").json()["status"] == "buffered"
        assert (
            client.post(receipt, json={"seq": 2, "played_samples": 2401, "status": "completed"}).status_code
            == 400
        )
        assert (
            client.post(
                receipt,
                json={
                    "seq": 2,
                    "played_samples": 1200,
                    "status": "playing",
                    "first_audio_ms": 1234.5,
                    "first_audio_scheduled": True,
                },
            ).status_code
            == 200
        )
        client.post(
            receipt,
            json={"seq": 1, "played_samples": 0, "status": "interrupted", "first_audio_scheduled": True},
        )
        assert client.get(f"/api/voice/playbacks/{pid}").json()["played_samples"] == 1200
        timing = client.get(f"/api/voice/playbacks/{pid}").json()["timings"]
        assert timing["first_audio_ms"] == 1234.5
        assert timing["first_audio_schedule_receipt_at"]
        assert (
            client.post(receipt, json={"seq": 3, "played_samples": 2400, "status": "completed"}).json()[
                "status"
            ]
            == "completed"
        )
        with db.session() as s:
            from chatbot.storage import Story
            from chatbot.story_timing import story_timing

            assert s.get(VoiceDelivery, (eid, player)).text_visible
            timeline = story_timing(s, s.get(Story, story))
            assert timeline["playbacks"][0]["first_audio_schedule_receipt_at"] == timing[
                "first_audio_schedule_receipt_at"
            ]
            charges = list(s.scalars(select(Usage).where(Usage.role == "tts")))
            assert len(charges) == 1 and charges[0].cost > 0 and charges[0].reserved == 0


def test_stale_epoch_scope_and_explicit_replay(app, runtime, db, config):
    with TestClient(app) as client:
        _, _, player, task, eid = setup(client, runtime, db, config)
        pid = prepare(client, player, task, eid).json()["id"]
        runtime.submit(player, config.workspace_owner, "改口", "new-turn")
        assert client.post(f"/api/voice/playbacks/{pid}/audio").status_code == 409
        assert prepare(client, player, task, eid, request_id="stale").status_code == 409
        fresh_epoch = client.get(f"/api/sessions/{player}").json()["epoch"]
        assert prepare(client, player, task, eid, request_id="auto", epoch=fresh_epoch).status_code == 409
        assert (
            prepare(
                client, player, task, eid, request_id="manual", epoch=fresh_epoch, replay=True
            ).status_code
            == 200
        )
        _, _, outsider = launch(client)
        assert client.post(f"/api/voice/sessions/{outsider}/utterances/{eid}/visible").status_code == 404


def test_new_page_explicit_replay_takes_over_only_inactive_old_playback(app, runtime, db, config):
    from datetime import timedelta

    from chatbot.storage import VoicePlayback, now

    with TestClient(app) as client:
        _, _, player, task, eid = setup(client, runtime, db, config)
        first = prepare(client, player, task, eid, client_id="old-page")
        assert first.status_code == 200
        pid = first.json()["id"]
        # A second tab cannot interrupt a page that is still sending receipts.
        blocked = prepare(
            client, player, task, eid,
            request_id="fresh-replay", replay=True, client_id="new-page",
        )
        assert blocked.status_code == 409
        with db.session.begin() as s:
            old = s.get(VoicePlayback, pid)
            old.timings = {
                **old.timings,
                "last_receipt_at": (now() - timedelta(seconds=5)).isoformat(),
            }
        takeover = prepare(
            client, player, task, eid,
            request_id="inactive-replay", replay=True, client_id="new-page",
        )
        assert takeover.status_code == 200
        with db.session() as s:
            old = s.get(VoicePlayback, pid)
            assert old.status == "interrupted"
            assert old.timings["interrupted_reason"] == "explicit_replay_takeover"
        # The previous page cannot finish an attempt that was taken over.
        completion = client.post(
            f"/api/voice/playbacks/{pid}/receipt",
            json={"seq": 1, "played_samples": 0, "status": "completed"},
        )
        assert completion.json()["status"] == "interrupted"


def test_interruption_keeps_partial_watermark_without_completion(app, runtime, db, config):
    with TestClient(app) as client:
        _, _, player, task, eid = setup(client, runtime, db, config)
        pid = prepare(client, player, task, eid).json()["id"]
        client.post(f"/api/voice/playbacks/{pid}/audio")
        url = f"/api/voice/playbacks/{pid}/receipt"
        client.post(url, json={"seq": 1, "played_samples": 600, "status": "interrupted"})
        client.post(url, json={"seq": 2, "played_samples": 900, "status": "playing"})
        client.post(url, json={"seq": 3, "played_samples": 2400, "status": "completed"})
        state = client.get(f"/api/voice/playbacks/{pid}").json()
        assert state["status"] == "interrupted"
        assert state["played_samples"] == 900


def test_tts_failure_never_counts_as_silence_or_audio_completion(app, runtime, db, config):
    async def broken(text, voice):
        yield b"\x00\x01" * 20
        raise ProviderError("failure")

    app.state.voice.pcm = broken
    with TestClient(app) as client:
        _, _, player, task, eid = setup(client, runtime, db, config)
        pid = prepare(client, player, task, eid).json()["id"]
        client.post(f"/api/voice/playbacks/{pid}/audio")
        state = client.get(f"/api/voice/playbacks/{pid}").json()
        assert state["status"] == "failed" and not state["stream_done"]
        assert client.get(f"/api/voice/sessions/{player}/utterances").json()["utterances"][0]["chunks"] == [
            "你好，今晚我们先聊聊。"
        ]


def test_shared_daily_budget_blocks_tts(app, runtime, db, config):
    config.daily_budget_usd = 0.000001
    with TestClient(app) as client:
        _, _, player, task, eid = setup(client, runtime, db, config)
        pid = prepare(client, player, task, eid).json()["id"]
        audio = client.post(f"/api/voice/playbacks/{pid}/audio")
        assert not audio.content
        assert client.get(f"/api/voice/playbacks/{pid}").json()["status"] == "failed"
        with db.session() as s:
            assert not list(s.scalars(select(Usage)))


def test_spoken_text_must_be_ordered_literal_and_exclude_control_tags():
    validate_spoken_parts("（点头）你好。再见。", ["你好。", "再见。"])
    for parts in [["再见。", "你好。"], ["额外的话"], ["（点头）"]]:
        with pytest.raises(ProviderError):
            validate_spoken_parts("（点头）你好。再见。", parts)


async def test_qwen_sse_decoding_and_missing_end(app):
    voice = app.state.voice
    from chatbot.voice import VoiceService

    frame = {"output": {"audio": {"data": base64.b64encode(b"\x00\x01").decode()}, "finish_reason": "stop"}}

    async def respond(request):
        return httpx.Response(200, text="data: " + json.dumps(frame) + "\n\n")

    await voice.http.aclose()
    voice.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    assert b"".join([p async for p in VoiceService.pcm(voice, "你好", "Cherry")]) == b"\x00\x01"
    frame["output"]["finish_reason"] = None
    with pytest.raises(ProviderError):
        _ = [p async for p in VoiceService.pcm(voice, "你好", "Cherry")]
    await voice.close()


def test_streaming_wav_header_is_removed_even_when_split():
    import io
    import wave

    from chatbot.voice import PCMDecoder

    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x01\x00" * 500)
    decoder = PCMDecoder()
    raw = data.getvalue()
    result = b"".join(decoder.feed(raw[i : i + 7]) for i in range(0, len(raw), 7)) + decoder.finish()
    assert result == b"\x01\x00" * 500
    decoder = PCMDecoder()
    decoder.feed(b"RIFFxxxxWAVE")
    with pytest.raises(ProviderError):
        decoder.finish()


async def test_invalid_speech_projection_keeps_reviewed_captions(db, config, runtime, fake):
    from chatbot.agents import Agents

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
        fake.replies["role_intent"] = {
            "act": "speak",
            "purpose": "回应",
            "addressee_id": player,
            "source_ids": [],
            "wait_for_player": True,
            "deception": False,
            "retrieval": "context",
        }
        fake.replies["dialogue"] = {"text": "你好。", "claims": [], "spoken_parts": ["[calm]你好。"]}
        runtime.submit(player, config.workspace_owner, "你好", "bad-projection")
        result = await Agents(db, config, runtime, fake).respond(runtime.claim("voice-test"))
        assert result["answer"] == "你好。"
        assert client.get(f"/api/voice/sessions/{player}/utterances").json()["utterances"][0]["chunks"] == []
