from datetime import timedelta

import pytest
from sqlalchemy import func, select

from chatbot.directing import DirectorPlan
from chatbot.director_state import install_plan, rebase_ack_plan, save_candidate
from chatbot.runtime import StaleTask
from chatbot.storage import Conversation, Operation, RoleMemory, SceneRecord, Story, Task, now


@pytest.fixture
def ledger(db):
    with db.session.begin() as s:
        story = Story(owner_id="local", title="导演测试")
        s.add(story)
        s.flush()
        player = Conversation(owner_id="local", title="玩家", story_id=story.id)
        s.add(player)
        s.flush()
        task = Task(
            owner_id="local",
            session_id=player.id,
            kind="respond",
            status="running",
            epoch=player.epoch,
            lease_version=1,
            lease_until=now() + timedelta(minutes=2),
            deadline=now() + timedelta(minutes=2),
        )
        s.add(task)
        s.flush()
    plan = DirectorPlan(
        beat_id="b",
        reason="交换线索",
        jobs=[
            dict(id="a", actor="npc1", objective="解释"),
            dict(id="b", actor="npc2", objective="回应", depends_on=["a"]),
            dict(id="c", actor="npc3", objective="追问", depends_on=["b"]),
            dict(id="d", actor="npc3", objective="独立观察"),
        ],
    )
    install_plan(db, task, story.id, 0, plan)
    return task, story.id, plan


def put(db, ledger, job, deps=None, revision=0, plan_revision=1):
    task, sid, _ = ledger
    return save_candidate(
        db,
        task,
        sid,
        0,
        plan_revision=plan_revision,
        job_id=job,
        expected_revision=revision,
        dependency_revisions=deps or {},
        candidate={"answer": "尚未演出的台词"},
    )


def test_private_candidates_persist_and_invalidate_only_descendants(db, ledger):
    put(db, ledger, "a")
    put(db, ledger, "b", {"a": 1})
    put(db, ledger, "c", {"b": 1})
    put(db, ledger, "d")
    put(db, ledger, "a", revision=1)
    with db.session() as s:
        data = s.get(Operation, f"{ledger[0].id}:director-ledger").payload
        assert data["candidates"]["a"]["revision"] == 2
        assert data["candidates"]["d"]["status"] == "approved"
        for job in ["b", "c"]:
            assert data["candidates"][job]["status"] == "expired"
            assert "value" not in data["candidates"][job]
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == 0
        assert s.scalar(select(func.count()).select_from(RoleMemory)) == 0
    with pytest.raises(StaleTask):
        put(db, ledger, "b", {"a": 1}, revision=1)
    with pytest.raises(StaleTask):
        put(db, ledger, "c", {"b": 1}, revision=1)
    put(db, ledger, "b", {"a": 2}, revision=1)
    put(db, ledger, "c", {"b": 2}, revision=1)


def test_replacement_and_stale_writers_cannot_overwrite(db, ledger):
    put(db, ledger, "a")
    with pytest.raises(StaleTask):
        put(db, ledger, "a")
    task, sid, plan = ledger
    with pytest.raises(StaleTask):
        install_plan(db, task, sid, 0, plan)
    new = install_plan(db, task, sid, 0, plan, expected_revision=1)
    assert new["revision"] == 2 and not new["candidates"]
    with pytest.raises(StaleTask):
        put(db, ledger, "a")
    put(db, ledger, "a", plan_revision=2)
    recovered = install_plan(db, task, sid, 0, plan, expected_revision=2, recovery_reason="state_changed")
    assert recovered["recovery_count"] == 1
    continued = install_plan(db, task, sid, 0, plan, expected_revision=3)
    assert continued["recovery_count"] == 1
    with pytest.raises(StaleTask, match="重规划次数"):
        install_plan(db, task, sid, 0, plan, expected_revision=4, recovery_reason="draft_rejected")


def test_concurrent_candidate_writers_have_one_winner(db, ledger):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    gate = Barrier(2)

    def write():
        gate.wait(timeout=5)
        try:
            put(db, ledger, "a")
            return "saved"
        except StaleTask:
            return "stale"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(write), pool.submit(write)
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == ["saved", "stale"]


@pytest.mark.parametrize("change", ["world", "epoch", "lease", "cancel"])
def test_live_state_changes_reject_candidate_without_writes(db, ledger, change):
    task, sid, _ = ledger
    with db.session.begin() as s:
        if change == "world":
            s.get(Story, sid).seq += 1
        elif change == "epoch":
            s.get(Conversation, task.session_id).epoch += 1
        elif change == "lease":
            s.get(Task, task.id).lease_version += 1
        else:
            s.get(Task, task.id).status = "cancelled"
    with pytest.raises(StaleTask):
        put(db, ledger, "a")
    with db.session() as s:
        assert not s.get(Operation, f"{task.id}:director-ledger").payload["candidates"]


def test_own_ack_can_advance_plan_and_candidate_without_accepting_other_world_changes(db, ledger):
    task, sid, plan = ledger
    with db.session.begin() as s:
        story = s.get(Story, sid)
        story.seq = 1
        s.add(
            SceneRecord(
                story_id=sid,
                seq=1,
                kind="ack_candidate",
                actor_name="npc",
                text="嗯。",
                dedupe=f"ack:{task.id}",
                task_id=task.id,
                payload={"acknowledgement": True},
            )
        )
        rebase_ack_plan(s, task, story, 0)
    put(db, ledger, "a")
    with db.session.begin() as s:
        story = s.get(Story, sid)
        row = s.scalar(select(SceneRecord).where(SceneRecord.dedupe == f"ack:{task.id}"))
        story.seq, row.seq, row.kind = 2, 2, "speech"
        rebase_ack_plan(s, task, story, 1)
    # A caller that observed seq 1 just before release can still save safely.
    save_candidate(
        db,
        task,
        sid,
        1,
        plan_revision=1,
        job_id="d",
        expected_revision=0,
        dependency_revisions={},
        candidate={"answer": "另一段"},
    )
    new = install_plan(db, task, sid, 0, plan, expected_revision=1, allow_ack_advance=True)
    assert new["world_seq"] == 2
    with db.session.begin() as s:
        s.get(Story, sid).seq = 3
        s.add(SceneRecord(story_id=sid, seq=3, kind="host", text="别的事件", dedupe="other"))
    with pytest.raises(StaleTask):
        save_candidate(
            db,
            task,
            sid,
            2,
            plan_revision=2,
            job_id="a",
            expected_revision=0,
            dependency_revisions={},
            candidate={"answer": "过时"},
        )
