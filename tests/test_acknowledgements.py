# ruff: noqa: F811
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch
from support.voice import app as app  # noqa: F401

from chatbot.acknowledgements import generate_acknowledgement
from chatbot.agents import Agents
from chatbot.observations import append_record, visible_records
from chatbot.storage import Conversation, Message, RoleMemory, SceneRecord, Story


async def candidate(client, runtime, db, config, fake):
    story, actors, player = launch(client)
    runtime.submit(
        player,
        config.workspace_owner,
        "我有件事想告诉你",
        "ack-input",
        channel="private",
        addressees=[actors[0]],
        acknowledgement=True,
    )
    task = runtime.claim("ack-test")
    fake.replies["acknowledgement"] = "嗯，我听着。"
    await generate_acknowledgement(Agents(db, config, runtime, fake), task)
    with db.session() as s:
        row = s.scalar(select(SceneRecord).where(SceneRecord.kind == "ack_candidate"))
    return story, actors, player, task, row


@pytest.mark.asyncio
async def test_broadcast_ack_uses_last_player_visible_speaker(app, runtime, db, config, fake):
    with TestClient(app) as c:
        story, actors, player = launch(c)
        with db.session.begin() as s:
            speaker = s.get(Conversation, actors[1])
            append_record(
                s,
                story_id=story,
                kind="speech",
                text="这件事你怎么看？",
                observer_ids=[speaker.id, player],
                actor=speaker,
                dedupe="last-heard",
                addressees=[player],
            )
            # A later private statement the player did not hear cannot take precedence.
            hidden = s.get(Conversation, actors[2])
            append_record(
                s,
                story_id=story,
                kind="speech",
                text="我另有打算。",
                observer_ids=[hidden.id],
                actor=hidden,
                dedupe="not-heard",
                channel="private",
            )
        runtime.submit(player, config.workspace_owner, "我想先试试。", "broadcast", acknowledgement=True)
        task = runtime.claim("ack-broadcast")
        fake.replies["acknowledgement"] = "嗯，我听着。"
        await generate_acknowledgement(Agents(db, config, runtime, fake), task)
        with db.session() as s:
            row = s.scalar(select(SceneRecord).where(SceneRecord.kind == "ack_candidate"))
        assert row and row.actor_id == actors[1]
        assert "我另有打算" not in fake.calls[0][1][-1]["content"]


@pytest.mark.asyncio
async def test_group_request_ack_uses_last_heard_named_speaker(app, runtime, db, config, fake):
    with TestClient(app) as c:
        story, actors, player = launch(c)
        with db.session.begin() as s:
            speaker = s.get(Conversation, actors[1])
            append_record(
                s,
                story_id=story,
                kind="speech",
                text="先听我说。",
                observer_ids=[speaker.id, player],
                actor=speaker,
                dedupe="group-last-heard",
                addressees=[player],
            )
            names = [s.get(Conversation, aid).current_state["profile"]["name"] for aid in actors]
        runtime.submit(
            player,
            config.workspace_owner,
            "、".join(names) + "，你们先彼此说清。",
            "ack-group",
            acknowledgement=True,
        )
        task = runtime.claim("ack-group")
        fake.replies["acknowledgement"] = "嗯，先说清楚。"
        await generate_acknowledgement(Agents(db, config, runtime, fake), task)
        with db.session() as s:
            row = s.scalar(select(SceneRecord).where(SceneRecord.kind == "ack_candidate"))
        assert row and row.actor_id == actors[1]


@pytest.mark.asyncio
async def test_group_request_without_prior_speaker_uses_first_named_visible_actor(
    app, runtime, db, config, fake
):
    with TestClient(app) as c:
        _, actors, player = launch(c)
        with db.session() as s:
            names = [s.get(Conversation, aid).current_state["profile"]["name"] for aid in actors[:2]]
        runtime.submit(
            player,
            config.workspace_owner,
            f"{names[1]}、{names[0]}，你们先聊聊。",
            "ack-group-no-prior",
            acknowledgement=True,
        )
        task = runtime.claim("ack-group-no-prior")
        fake.replies["acknowledgement"] = "嗯，我听着。"
        await generate_acknowledgement(Agents(db, config, runtime, fake), task)
        with db.session() as s:
            row = s.scalar(select(SceneRecord).where(SceneRecord.kind == "ack_candidate"))
        assert row and row.actor_id == actors[1]


@pytest.mark.asyncio
async def test_quick_ack_rejects_substantive_stance_without_publishing_it(
    app, runtime, db, config, fake
):
    with TestClient(app) as c:
        _, actors, player = launch(c)
        runtime.submit(
            player,
            config.workspace_owner,
            "强风时你们怎么看？",
            "ack-substantive",
            addressees=[actors[0]],
            acknowledgement=True,
        )
        task = runtime.claim("ack-substantive")
        fake.replies["acknowledgement"] = "强风天，我不会拿船冒险。"
        await generate_acknowledgement(Agents(db, config, runtime, fake), task)
        with db.session() as s:
            assert not s.scalar(select(SceneRecord).where(SceneRecord.dedupe == f"ack:{task.id}"))


@pytest.mark.asyncio
async def test_broadcast_ack_skips_unseen_actor(app, runtime, db, config, fake):
    with TestClient(app) as c:
        story, actors, player = launch(c)
        with db.session.begin() as s:
            p = s.get(Conversation, player)
            speaker = s.get(Conversation, actors[1])
            append_record(
                s,
                story_id=story,
                kind="speech",
                text="我在这里。",
                observer_ids=[speaker.id, player],
                actor=speaker,
                dedupe="unnoticed",
            )
            # Concealment after a speech prevents a later automatic reply.
            p.current_state = {**p.current_state, "unnoticed_ids": [actors[1]]}
        runtime.submit(player, config.workspace_owner, "有人吗？", "unseen-broadcast", acknowledgement=True)
        task = runtime.claim("ack-unseen")
        fake.replies["acknowledgement"] = "嗯。"
        await generate_acknowledgement(Agents(db, config, runtime, fake), task)
        with db.session() as s:
            assert not s.scalar(select(SceneRecord).where(SceneRecord.kind == "ack_candidate"))


@pytest.mark.asyncio
async def test_ack_drops_candidate_if_world_changes_during_generation(app, runtime, db, config, fake):
    with TestClient(app) as c:
        story, actors, player = launch(c)
        runtime.submit(
            player, config.workspace_owner, "我有件事想告诉你", "ack-stale-world",
            channel="private", addressees=[actors[0]], acknowledgement=True,
        )
        task = runtime.claim("ack-stale-world")

        def changed(_):
            with db.session.begin() as s:
                append_record(
                    s, story_id=story, kind="host", text="场景出现新公告。",
                    observer_ids=[], dedupe="intervening-world-change",
                )
            return "嗯，我听着。"

        fake.replies["acknowledgement"] = changed
        await generate_acknowledgement(Agents(db, config, runtime, fake), task)
        with db.session() as s:
            assert not s.scalar(select(SceneRecord).where(SceneRecord.dedupe == f"ack:{task.id}"))


@pytest.mark.asyncio
async def test_ack_is_not_an_observation_until_release_and_keeps_private_scope(
    app, runtime, db, config, fake
):
    with TestClient(app) as c:
        story, actors, player, task, row = await candidate(c, runtime, db, config, fake)
        assert row
        with db.session() as s:
            before_seq = s.get(Story, story).seq
            assert row.seq is None
            assert not s.scalar(select(RoleMemory).where(RoleMemory.source_ids.contains([row.id])))
            assert row.id not in {e.id for e in visible_records(s, actors[0])}
        pending = c.get(f"/api/voice/sessions/{player}/utterances?after={before_seq}").json()
        assert any(u["id"] == row.id and u["seq"] == 0 for u in pending["utterances"])
        p = c.post(
            f"/api/voice/sessions/{player}/playbacks",
            json={"request_id": "ack-play", "event_id": row.id, "epoch": task.epoch, "chunk_index": 0},
        ).json()
        assert c.post(f"/api/voice/playbacks/{p['id']}/start", json={}).status_code == 409
        assert c.post(f"/api/voice/playbacks/{p['id']}/audio").status_code == 200
        assert (
            c.post(f"/api/voice/playbacks/{p['id']}/start", json={"send_to_release_ms": 500}).status_code
            == 200
        )
        assert c.post(f"/api/voice/playbacks/{p['id']}/start", json={}).status_code == 200
        with db.session() as s:
            assert s.get(Story, story).seq == before_seq + 1
            assert s.get(SceneRecord, row.id).seq == before_seq + 1
            assert row.id in {e.id for e in visible_records(s, actors[0])}
            assert row.id not in {e.id for e in visible_records(s, actors[1])}
            assert len(list(s.scalars(select(Message).where(Message.id == row.id)))) == 1
        assert [call[0] for call in fake.calls] == ["acknowledgement"]


@pytest.mark.asyncio
async def test_main_wins_before_ack_release(app, runtime, db, config, fake):
    with TestClient(app) as c:
        _, actors, player, task, row = await candidate(c, runtime, db, config, fake)
        p = c.post(
            f"/api/voice/sessions/{player}/playbacks",
            json={"request_id": "ack", "event_id": row.id, "epoch": task.epoch, "chunk_index": 0},
        ).json()
        c.post(f"/api/voice/playbacks/{p['id']}/audio")
        with db.session() as s:
            revision = s.get(Conversation, actors[0]).revision
        runtime.commit_role_segment(
            task,
            actors[0],
            "你说吧。",
            step=0,
            revision=revision,
            intent={"act": "speak"},
            spoken_parts=["你说吧。"],
        )
        rejected = c.post(f"/api/voice/playbacks/{p['id']}/start", json={})
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "ack_superseded"
        with db.session() as s:
            assert not s.scalar(select(RoleMemory).where(RoleMemory.source_ids.contains([row.id])))


@pytest.mark.asyncio
async def test_ack_invalidated_by_new_input(app, runtime, db, config, fake):
    with TestClient(app) as c:
        _, _, player, task, row = await candidate(c, runtime, db, config, fake)
        runtime.submit(player, config.workspace_owner, "等一下", "new-input")
        r = c.post(
            f"/api/voice/sessions/{player}/playbacks",
            json={"request_id": "late", "event_id": row.id, "epoch": task.epoch, "chunk_index": 0},
        )
        assert r.status_code in {404, 409}
        with db.session() as s:
            assert not s.scalar(select(RoleMemory).where(RoleMemory.source_ids.contains([row.id])))


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["new_input", "redacted"])
async def test_streaming_private_ack_new_epoch_is_interrupted_not_failed(app, runtime, db, config, fake, cause):
    from chatbot.storage import Usage, VoicePlayback

    class Connected:
        async def is_disconnected(self):
            return False

    with TestClient(app) as c:
        _, _, player, task, row = await candidate(c, runtime, db, config, fake)
        response = c.post(
            f"/api/voice/sessions/{player}/playbacks",
            json={"request_id": "private-stream", "event_id": row.id, "epoch": task.epoch, "chunk_index": 0},
        )
        assert response.status_code == 200
        pid = response.json()["id"]
        stream = app.state.voice.stream(pid, Connected())
        assert await anext(stream)
        if cause == "new_input":
            runtime.submit(player, config.workspace_owner, "先换个话题", "new-epoch")
        else:
            with db.session.begin() as session:
                session.get(SceneRecord, row.id).redacted = True
        assert await anext(stream, None) is None
        with db.session() as session:
            playback = session.get(VoicePlayback, pid)
            if cause == "new_input":
                assert playback.status == "interrupted"
                assert playback.timings["interrupted_reason"] == "epoch_changed"
                assert "failure" not in playback.timings
            else:
                assert playback.status == "failed"
                assert playback.timings["error"] == "HTTPException"
                assert "interrupted_reason" not in playback.timings
            assert playback.sent_samples > 0 and not playback.stream_done
            assert session.get(SceneRecord, row.id).kind == "ack_candidate"
            assert session.get(SceneRecord, row.id).seq is None
            charges = list(session.scalars(select(Usage).where(Usage.role == "tts")))
            assert charges and all(charge.reserved == 0 for charge in charges)


@pytest.mark.asyncio
async def test_main_prefetch_waits_for_started_ack(app, runtime, db, config, fake):
    with TestClient(app) as c:
        _, actors, player, task, row = await candidate(c, runtime, db, config, fake)

        def prepare(eid, rid):
            return c.post(
                f"/api/voice/sessions/{player}/playbacks",
                json={
                    "request_id": rid,
                    "event_id": eid,
                    "epoch": task.epoch,
                    "chunk_index": 0,
                    "client_id": "browser",
                    "prefetch": True,
                },
            )

        first = prepare(row.id, "ack").json()["id"]
        assert c.post(f"/api/voice/playbacks/{first}/audio").status_code == 200
        assert c.post(f"/api/voice/playbacks/{first}/start", json={}).status_code == 200
        with db.session() as s:
            revision = s.get(Conversation, actors[0]).revision
        eid = runtime.commit_role_segment(
            task,
            actors[0],
            "说吧。",
            step=0,
            revision=revision,
            intent={"act": "speak"},
            spoken_parts=["说吧。"],
        )
        response = prepare(eid, "main")
        assert response.status_code == 200, response.text
        second = response.json()["id"]
        assert c.post(f"/api/voice/playbacks/{second}/audio").status_code == 200
        assert c.post(f"/api/voice/playbacks/{second}/start", json={}).status_code == 409
        assert prepare(eid, "third").status_code == 409
        assert (
            c.post(
                f"/api/voice/playbacks/{first}/receipt",
                json={"seq": 1, "status": "completed", "played_samples": 2400},
            ).status_code
            == 200
        )
        assert c.post(f"/api/voice/playbacks/{second}/start", json={}).status_code == 200
