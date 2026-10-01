import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from support.story import launch

from chatbot.api import create_app
from chatbot.observations import append_record
from chatbot.role_memory import RoleMemoryService
from chatbot.storage import Conversation, SceneRecord


def test_player_statement_comparison_keeps_order_and_repetition_without_snapshot(db, config, runtime, fake):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    with db.session.begin() as s:
        person = s.get(Conversation, player)
        for index, message in enumerate(
            ["有人说账册在医药箱。", "医药箱只是转述，不是我亲眼看到。", "医药箱只是转述，不是我亲眼看到。"]
        ):
            append_record(
                s,
                story_id=person.story_id,
                kind="speech",
                actor=person,
                text=message,
                observer_ids=[player, actors[0]],
                dedupe=f"history:{index}",
            )
        other = s.get(Conversation, actors[1])
        append_record(
            s,
            story_id=person.story_id,
            kind="speech",
            actor=other,
            text="医药箱里有绝密材料。",
            observer_ids=[actors[1]],
            dedupe="private-history",
        )
    runtime.submit(
        player,
        config.workspace_owner,
        "我之前对医药箱的说法分别是什么？请按时间引用最初说法和后来纠正，不要用你的复述代替我的原话。",
        "compare",
        addressees=[actors[0]],
    )
    task = runtime.claim("foreground")
    _, info = RoleMemoryService(db, config, runtime, fake).context(task, actors[0])
    history = info["player_statement_history"]
    assert len(history["items"]) == 3
    assert [e["seq"] for e in history["items"]] == sorted(e["seq"] for e in history["items"])
    assert history["items"][1]["text"] == history["items"][2]["text"]
    assert all(e["actor_id"] == player for e in history["items"])
    assert not history["more_matching_events"]


@pytest.mark.parametrize("reserve_tokens", [6000, 10000])
def test_large_optional_tail_does_not_wait_for_snapshot(db, config, runtime, fake, reserve_tokens):
    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, actors, player = launch(client)
    with db.session.begin() as s:
        person = s.get(Conversation, actors[0])
        for i in range(60):
            append_record(
                s,
                story_id=person.story_id,
                kind="speech",
                actor=person,
                text=f"第{i}段无关旧话。" + "当时大家聊起窗外的雨声。" * 20,
                observer_ids=[person.id, player],
                dedupe=f"long:{i}",
            )
    runtime.submit(player, config.workspace_owner, "请只回应我现在的问题", "now", addressees=[actors[0]])
    task = runtime.claim("foreground")
    context, info = RoleMemoryService(db, config, runtime, fake).context(
        task, actors[0], reserve_tokens=reserve_tokens
    )
    assert info["tokens"] <= config.context_hard_tokens - reserve_tokens
    assert task.payload["event_id"] in {e["id"] for e in info["observations"]}
    assert "省略的历史仍在档案" in context[2]["content"]
    with db.session() as s:
        assert len(list(s.scalars(select(SceneRecord).where(SceneRecord.dedupe.like("long:%"))))) == 60
