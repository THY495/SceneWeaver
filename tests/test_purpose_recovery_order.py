"""Changing recovery order never changes the factual publication gates."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import intention, launch

from chatbot.agents import Agents
from chatbot.api import create_app
from chatbot.npc import turns
from chatbot.npc.performance import UnsafeDraftExhausted
from chatbot.storage import SceneRecord

BAD = "公告栏在检票口左边。"
GOOD = "我现在不清楚公告栏的位置。"
REJECTED = {"accepted": False, "issues": ["无位置依据"], "checks": [
    {"text": BAD, "basis": "没有位置", "supported": False, "reason_code": "unsupported_fact"},
]}
ACCEPTED = {"accepted": True, "issues": [], "checks": []}


@pytest.mark.parametrize("order,expected_intents", [("immediate", 2), ("after_local_repair", 1)])
async def test_single_local_fix_can_skip_purpose_call_without_skipping_review(db, config, runtime, fake, monkeypatch, order, expected_intents):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    original = turns.perform_actor
    async def perform(*args, **kwargs):
        return await original(*args, **kwargs, purpose_recovery_order=order)
    monkeypatch.setattr(turns, "perform_actor", perform)
    intents = iter([intention(player, purpose="承认当前不知道公告栏位置"), {"purpose": "承认当前不知道公告栏位置"}])
    speeches = iter([{"text": BAD, "claims": []}, {"text": GOOD, "claims": []}])
    reviews = iter([REJECTED, ACCEPTED])
    fake.replies.update(role_intent=lambda _: next(intents), dialogue=lambda _: next(speeches), actor_review=lambda _: next(reviews))
    runtime.submit(player, config.workspace_owner, "公告栏在哪儿？", "recovery-order")
    result = await Agents(db, config, runtime, fake).respond(runtime.claim("foreground"))
    assert result["answer"] == GOOD
    assert sum(role == "role_intent" for role, *_ in fake.calls) == expected_intents
    assert sum(role == "dialogue" for role, *_ in fake.calls) == 2
    assert sum(role == "actor_review" for role, *_ in fake.calls) == 2


async def test_deferred_purpose_is_once_and_failed_revisions_never_publish(db, config, runtime, fake, monkeypatch):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    original = turns.perform_actor
    async def perform(*args, **kwargs):
        return await original(*args, **kwargs, purpose_recovery_order="after_local_repair")
    monkeypatch.setattr(turns, "perform_actor", perform)
    intents = iter([intention(player, purpose="说明公告栏方位"), {"purpose": "仍说明公告栏方位"}])
    fake.replies.update(role_intent=lambda _: next(intents), dialogue={"text": BAD, "claims": []}, actor_review=REJECTED)
    runtime.submit(player, config.workspace_owner, "公告栏在哪儿？", "recovery-order-failure")
    task = runtime.claim("foreground")
    with pytest.raises(UnsafeDraftExhausted):
        await Agents(db, config, runtime, fake).respond(task)
    roles = [role for role, *_ in fake.calls if role in {"role_intent", "dialogue", "actor_review"}]
    assert roles == ["role_intent", "dialogue", "actor_review", "dialogue", "actor_review", "role_intent", "dialogue", "actor_review"]
    with db.session() as s:
        assert not s.scalar(select(SceneRecord.id).where(
            SceneRecord.task_id == task.id, SceneRecord.kind == "speech", SceneRecord.actor_id != player,
        ))
