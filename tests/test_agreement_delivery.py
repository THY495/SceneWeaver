import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select
from support.agreements import consent, setup

from chatbot.agreement_delivery import deliver_notices
from chatbot.agreements import proposals
from chatbot.storage import AgreementEvent, AgreementNotice, Conversation, Event, Observation, SceneRecord


def test_outbox_rollback_recovery_and_no_duplicate_delivery(db, config, runtime, fake, monkeypatch):
    _, _, _, _, _ = setup(db, config, fake)
    with db.session() as s:
        count = s.scalar(select(func.count()).select_from(AgreementNotice))
        observations = s.scalar(select(func.count()).select_from(Observation))
    original = runtime.emit

    def fail_after_emit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated process interruption before commit")

    monkeypatch.setattr(runtime, "emit", fail_after_emit)
    with pytest.raises(RuntimeError):
        deliver_notices(db, runtime)
    with db.session() as s:
        assert not s.scalar(select(Event).where(Event.kind == "agreement_updated"))
        assert (
            s.scalar(
                select(func.count()).select_from(AgreementNotice).where(AgreementNotice.status == "pending")
            )
            == count
        )
    monkeypatch.setattr(runtime, "emit", original)
    assert deliver_notices(db, runtime) == count
    assert deliver_notices(db, runtime) == 0
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(Observation)) == observations
        assert (
            s.scalar(select(func.count()).select_from(Event).where(Event.kind == "agreement_updated"))
            == count
        )


def test_deleted_event_cancels_unsent_notifications(db, config, runtime, fake):
    _, _, _, _, _ = setup(db, config, fake)
    with db.session.begin() as s:
        for index in s.scalars(select(AgreementEvent)):
            s.get(SceneRecord, index.event_id).redacted = True
    assert deliver_notices(db, runtime) == 0
    with db.session() as s:
        assert {notice.status for notice in s.scalars(select(AgreementNotice))} == {"cancelled"}


def test_game_scope_leaves_other_story_outbox_untouched(db, config, runtime, fake):
    from chatbot.game_scope import GameExecutionScope
    from chatbot.storage import GameSave

    story, _, player, _, _ = setup(db, config, fake)
    runtime.execution_scope = GameExecutionScope(config.workspace_owner, "release-a")
    with db.session() as s:
        pending = s.scalar(select(func.count()).select_from(AgreementNotice))
    assert pending > 0
    assert deliver_notices(db, runtime) == 0
    with db.session.begin() as s:
        assert {n.status for n in s.scalars(select(AgreementNotice))} == {"pending"}
        s.add(
            GameSave(
                player_id=player,
                story_id=story,
                owner_id=config.workspace_owner,
                release_id="release-a",
                request_id="save",
                label="Save",
            )
        )
    assert deliver_notices(db, runtime) == pending


def test_state_replay_and_migration_backfill_does_not_renotify(db, config, fake):
    story, actors, player, _, data = setup(db, config, fake)
    iid = data["items"][0]["id"]
    with db.session.begin() as s:
        a, b = [s.get(Conversation, aid) for aid in actors[:2]]
        assert proposals(s, story, a.id)[0]["item_states"][iid]["state"] == "wait_reply"
        consent(s, a, data)
        consent(s, b, data, decision="refuse", request="refuse")
        assert proposals(s, story, a.id)[0]["item_states"][iid]["state"] == "refused"
        consent(s, b, data)
        assert proposals(s, story, a.id)[0]["item_states"][iid]["state"] == "ready"
        consent(s, b, data, decision="withdraw", request="withdraw")
        assert proposals(s, story, a.id)[0]["item_states"][iid]["state"] == "cancelled"
        assert proposals(s, story, player) == []
        before = proposals(s, story, a.id)
    spec = importlib.util.spec_from_file_location(
        "agreement_migration", Path("migrations/versions/009_agreement_journal.py")
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with db.engine.begin() as conn:
        migration.op = Operations(MigrationContext.configure(conn))
        AgreementNotice.__table__.drop(conn)
        AgreementEvent.__table__.drop(conn)
        migration.upgrade()
    with db.session() as s:
        assert proposals(s, story, actors[0]) == before
        assert not list(s.scalars(select(AgreementNotice)))


def test_deleted_latest_decision_cannot_resurrect_earlier_consent(db, config, fake):
    story, actors, _, _, data = setup(db, config, fake)
    with db.session.begin() as s:
        a, b = [s.get(Conversation, aid) for aid in actors[:2]]
        consent(s, a, data)
        consent(s, b, data)
        refusal = consent(s, b, data, decision="refuse", request="latest-refusal")
        refusal.redacted, refusal.text, refusal.payload = True, "[已删除]", {}
    with db.session() as s:
        assert proposals(s, story, actors[0]) == []
        assert proposals(s, story) == []


async def test_npc_can_propose_from_observed_player_request(db, config, runtime, fake):
    from chatbot.agents import Agents

    story, actors, player, _, _ = setup(db, config, fake)
    runtime.submit(player, config.workspace_owner, "馆长，请考虑把铜盒交给修复师。", "automatic-proposal")
    task = runtime.claim("proposal-test")
    fake.replies["role_intent"] = {
        "act": "agreement_proposal",
        "source_ids": [],
        "deception": False,
        "addressee_id": None,
        "wait_for_player": True,
        "retrieval": "context",
        "purpose": "提出可讨论的交接安排",
        "agreement_proposal": {
            "source_event_id": task.payload["event_id"],
            "items": [
                {
                    "key": "transfer",
                    "executor_id": actors[0],
                    "action": {
                        "object_id": "box",
                        "verb": "give",
                        "expected_version": 1,
                        "target_id": actors[1],
                    },
                }
            ],
        },
    }
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["answer"] == ""
    with db.session() as s:
        created = next(
            p for p in proposals(s, story, actors[0]) if p["source_event_id"] == task.payload["event_id"]
        )
        assert created["decisions"] == {}  # Request and proposal never auto-accept for anyone.
        assert created["initiator_id"] == actors[0]
        assert proposals(s, story, player) == []
        assert s.get(AgreementEvent, created["event_id"])
        event_id = created["event_id"]
    # Existing task/step receipt wins on retry before stale revision checks.
    assert runtime.commit_role_segment(task, actors[0], "ignored", step=0, revision=-1, intent={}) == event_id
