from datetime import timedelta

import pytest
from sqlalchemy import select

from chatbot.game_scope import GameExecutionScope
from chatbot.runtime import Runtime, StaleTask
from chatbot.storage import Conversation, GameSave, Story, Task, now


def test_release_claim_check_and_expiry_do_not_touch_other_saves(db, config):
    owner = config.workspace_owner
    with db.session.begin() as s:
        for key, release in [("ours", "release-a"), ("other", "release-b")]:
            s.add(Story(id=key, owner_id=owner, title=key))
            s.flush()
            s.add(Conversation(id=key, story_id=key, owner_id=owner, title=key))
            s.flush()
            s.add(
                GameSave(
                    player_id=key, story_id=key, owner_id=owner, release_id=release, request_id=key, label=key
                )
            )
        for key, session, kind, payload in [
            ("foreign", "other", "respond", {}),
            ("good", "ours", "respond", {}),
            ("bad-scene", "ours", "scene_run", {"story_id": "other"}),
            ("good-scene", None, "scene_run", {"story_id": "ours"}),
        ]:
            s.add(
                Task(
                    id=key,
                    owner_id=owner,
                    session_id=session,
                    kind=kind,
                    payload=payload,
                    deadline=now() + timedelta(minutes=5),
                )
            )
    runtime = Runtime(db, config, execution_scope=GameExecutionScope(owner, "release-a"))
    assert runtime.claim("worker").id == "good"
    with pytest.raises(StaleTask, match="当前发行作品"):
        runtime.check("foreign")
    with db.session.begin() as s:
        s.get(Task, "good").status = "completed"
    assert runtime.claim("worker", background=True).id == "good-scene"
    with db.session.begin() as s:
        for task in s.scalars(select(Task)):
            task.deadline = now() - timedelta(seconds=1)
    runtime.expire()
    with db.session() as s:
        assert s.get(Task, "foreign").status == "queued"
        assert s.get(Task, "bad-scene").status == "queued"
        assert s.get(Task, "good-scene").status == "failed"
    # A running host sees new catalogue rows without restarting or broadening scope.
    with db.session.begin() as s:
        s.get(GameSave, "other").release_id = "release-a"
        s.get(Task, "foreign").deadline = now() + timedelta(minutes=5)
    assert runtime.claim("worker").id == "foreign"
