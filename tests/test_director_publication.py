import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from support.story import launch

from chatbot.api import create_app
from chatbot.directing import DirectorPlan
from chatbot.director_state import CandidateCommit, install_plan, save_candidate
from chatbot.runtime import StaleTask
from chatbot.storage import Conversation, Operation, SceneRecord, Story, now
from chatbot.voice_candidate import CandidateAudio


def test_publication_is_ordered_atomic_bound_to_review_and_retryable(db, config, runtime, fake, monkeypatch):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        sid, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "大家说说看", "director-commit")
    task = runtime.claim("test")
    with db.session() as s:
        world = s.get(Story, sid).seq
        count = s.scalar(select(func.count()).select_from(SceneRecord))
    plan = DirectorPlan(
        beat_id="b",
        reason="轮流讨论",
        jobs=[
            dict(id="a", actor=actors[0], objective="提问"),
            dict(id="b", actor=actors[1], objective="回应", depends_on=["a"]),
        ],
    )
    install_plan(db, task, sid, world, plan)

    def value(actor):
        with db.session() as s:
            revision = s.get(Conversation, actor).revision
        return dict(
            text="我们可以再讨论一下。",
            revision=revision,
            intent={"act": "speak", "purpose": "讨论", "wait_for_player": False},
            evidence=[],
            claims=[],
            spoken_parts=["我们可以再讨论一下。"],
            performance=[],
        )

    def save(job, val, seq, expected=0):
        save_candidate(
            db,
            task,
            sid,
            seq,
            plan_revision=1,
            job_id=job,
            expected_revision=expected,
            dependency_revisions={"a": 1} if job == "b" else {},
            candidate=val,
        )

    first, second = value(actors[0]), value(actors[1])
    save("a", first, world)
    save("b", second, world)

    def commit(index, val, token, audio_status="ready"):
        return runtime.commit_role_segment(
            task,
            actors[index],
            step=index,
            director_candidate=token,
            prepared_audio=CandidateAudio(val["text"], "test-voice", "", "test-scope", b"\x01\x00" * 100)
            if audio_status == "ready"
            else None,
            prepared_audio_status=audio_status,
            **val,
        )

    with pytest.raises(StaleTask, match="前序"):
        commit(1, second, CandidateCommit(1, "b", 1))
    with pytest.raises(StaleTask, match="内容或身份"):
        commit(0, {**first, "text": "未经审查的替换"}, CandidateCommit(1, "a", 1))
    with pytest.raises(StaleTask, match="必须绑定"):
        commit(0, first, None)
    with pytest.raises(StaleTask, match="候选语音"):
        commit(0, first, CandidateCommit(1, "a", 1), "pending")
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == count
    from chatbot import director_state

    original_mark = director_state.mark_publication

    def fail_after_mark(*args, **kwargs):
        original_mark(*args, **kwargs)
        raise RuntimeError("simulated failure before transaction commit")

    with monkeypatch.context() as patch:
        patch.setattr(director_state, "mark_publication", fail_after_mark)
        with pytest.raises(RuntimeError, match="simulated"):
            commit(0, first, CandidateCommit(1, "a", 1))
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == count
        assert s.get(Story, sid).seq == world
        assert not list(s.scalars(select(Operation).where(Operation.id.startswith("voice-ready:"))))
        assert (
            s.get(Operation, f"{task.id}:director-ledger").payload["candidates"]["a"]["status"] == "approved"
        )
    with db.session.begin() as s:
        row = s.get(Story, sid)
        row.settings = {**row.settings, "paused": True}
    assert commit(0, first, CandidateCommit(1, "a", 1)) is None
    with db.session() as s:
        gate = s.get(Operation, f"{task.id}:publication-gate:0")
        assert gate.payload == {"reason": "story_paused"}
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == count
    with db.session.begin() as s:
        row = s.get(Story, sid)
        row.settings = {**row.settings, "paused": False}
        player_row = s.get(Conversation, player)
        player_row.current_state = {
            **player_row.current_state,
            "presence": {"until": now().timestamp() + 60, "visible": True, "typing": True},
        }
    # Presence may change after submission while the player awaits this reply.
    event = commit(0, first, CandidateCommit(1, "a", 1))
    assert event and commit(0, first, CandidateCommit(1, "a", 1)) == event
    with db.session() as s:
        row = s.get(Operation, f"{task.id}:director-ledger").payload
        assert row["candidates"]["a"]["event_id"] == event
        assert row["world_seq"] == s.get(Story, sid).seq
        assert s.get(Operation, "voice-ready:" + event).payload["text"] == first["text"]
        assert s.get(SceneRecord, event).payload["voice_first_chunk"] == first["text"]
        assert s.get(SceneRecord, event).payload["presentation"] == {
            "version": 1,
            "release_id": event,
            "audio_status": "ready",
            "mode": "normal",
        }
        seq = row["world_seq"]
    with pytest.raises(StaleTask, match="不可修订"):
        save("a", first, seq, expected=1)
    # Re-prepare against the newly observed first speech before releasing b.
    second = value(actors[1])
    save("b", second, seq, expected=1)
    next_event = commit(1, second, CandidateCommit(1, "b", 2), "timeout")
    assert next_event and next_event != event
    with db.session() as s:
        assert s.get(SceneRecord, next_event).payload["presentation"]["mode"] == "text_fallback"
        assert s.get(Operation, "voice-ready:" + next_event) is None
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == count + 2
        seq = s.get(Story, sid).seq
    with TestClient(create_app(config, db, fake)) as client:
        utterances = client.get(f"/api/voice/sessions/{player}/utterances").json()["utterances"]
        fallback = next(row for row in utterances if row["id"] == next_event)
        assert fallback["text_fallback"] is True
        assert fallback["chunks"]  # Manual replay is still possible; auto playback must skip.
    replacement = DirectorPlan(
        beat_id="b",
        reason="继续向第三人询问",
        jobs=[
            dict(id="c", actor=actors[2], objective="补充"),
        ],
    )
    installed = install_plan(db, task, sid, seq, replacement, expected_revision=1)
    assert installed["step_offset"] == 2
    # A lost acknowledgement from the previous plan still resolves to its event.
    assert commit(0, first, CandidateCommit(1, "a", 1)) == event
    third = value(actors[2])
    save_candidate(
        db,
        task,
        sid,
        seq,
        plan_revision=2,
        job_id="c",
        expected_revision=0,
        dependency_revisions={},
        candidate=third,
    )
    with pytest.raises(StaleTask):
        runtime.commit_role_segment(
            task, actors[2], step=0, director_candidate=CandidateCommit(2, "c", 1), **third
        )
    assert runtime.commit_role_segment(
        task, actors[2], step=2, director_candidate=CandidateCommit(2, "c", 1), **third
    )
    with db.session() as s:
        history = s.get(Operation, f"{task.id}:director-ledger:history:1").payload
        assert history["candidates"]["a"]["event_id"] == event
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == count + 3
