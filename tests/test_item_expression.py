import pytest

from chatbot.npc.item_expression import (
    bind_result_quotes,
    expression_token_budget,
    result_candidates,
    result_focus,
)
from chatbot.providers import ProviderError


def test_result_focus_requires_actual_scoped_action_record():
    event = {
        "id": "one",
        "kind": "action",
        "text": "未打开。",
        "detail": {"item_action_result": {"status": "rejected", "code": "locked"}},
    }
    intent = {"act": "speak", "result_event_id": "one"}
    assert result_focus(intent, {"observations": [event]}) == event
    assert result_focus({"act": "speak"}, {}) is None
    for observations in [[], [{**event, "kind": "speech"}], [{**event, "detail": {}}]]:
        with pytest.raises(ProviderError):
            result_focus(intent, {"observations": observations})
    with pytest.raises(ProviderError):
        result_focus({**intent, "act": "item_action"}, {"observations": [event]})


def test_binding_preserves_claim_and_does_not_reassign_other_sources():
    event = {"id": "one", "text": "背包上锁，未打开。"}
    claim = {"text": "背包打开了", "source_id": "one", "quote": "paraphrase", "mode": "fact"}
    unrelated = {**claim, "source_id": "unknown"}
    speech = {"text": claim["text"], "claims": [claim, unrelated]}
    bound = bind_result_quotes(speech, event)
    assert bound["claims"][0] == {**claim, "quote": event["text"]}
    assert bound["claims"][1] == unrelated
    assert bound["text"] == speech["text"]  # Unsupported assertion remains for the reviewer.
    assert speech["claims"][0]["quote"] == "paraphrase"
    assert bind_result_quotes(speech, None) is speech
    assert bind_result_quotes(speech, {**event, "text": "x" * 1501}) is speech


def test_candidates_exclude_statements_and_unknown_actions():
    event = {
        "id": "one",
        "kind": "action",
        "text": "未打开。",
        "detail": {"item_action_result": {"status": "rejected", "code": "locked"}},
    }
    assert result_candidates(
        {"observations": [event, {**event, "kind": "speech"}, {**event, "detail": {}}]}
    ) == [{"id": "one", "text": "未打开。"}]
    assert result_candidates({}) == []
    assert expression_token_budget(event, {"observations": [event]}) == 600
    assert expression_token_budget(None, {"observations": [event]}) == 2000
    assert expression_token_budget(None, {}) == 1200


async def test_unseen_result_focus_is_repaired_before_dialogue(db, config, runtime, fake):
    from fastapi.testclient import TestClient
    from support.story import intention, launch

    from chatbot.agents import Agents
    from chatbot.api import create_app

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        _, _, player = launch(client)
    attempts = iter(
        [
            intention(player, result_event_id="unseen-result"),
            intention(player, result_event_id=None),
        ]
    )
    fake.replies["role_intent"] = lambda _: next(attempts)
    runtime.submit(player, config.workspace_owner, "请解释情况并讨论下一步方案。", "result-route-repair")
    task = runtime.claim("foreground")
    await Agents(db, config, runtime, fake).respond(task)
    assert len([c for c in fake.calls if c[0] == "role_intent"]) == 2
    assert len([c for c in fake.calls if c[0] == "dialogue"]) == 1
    repair = [c for c in fake.calls if c[0] == "role_intent"][-1][1][-1]["content"]
    assert "result_focus_invalid" in repair


async def test_focused_result_quote_binding_still_requires_semantic_review(db, config, runtime, fake):
    from fastapi.testclient import TestClient
    from support.story import intention, launch

    from chatbot.agents import Agents
    from chatbot.api import create_app
    from chatbot.observations import append_record
    from chatbot.storage import Conversation

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    with db.session.begin() as session:
        record = append_record(
            session,
            story_id=story,
            kind="action",
            text="背包上锁，未打开。",
            observer_ids=[actors[0]],
            actor=session.get(Conversation, actors[0]),
            dedupe="locked-result",
            payload={"item_action_result": {"status": "rejected", "code": "locked"}},
        )
        event_id = record.id
    fake.replies["role_intent"] = intention(player, source_ids=[event_id], result_event_id=event_id)
    speeches = iter(
        [
            {"text": text, "claims": [{"text": text, "source_id": event_id, "quote": "paraphrased"}]}
            for text in ["背包打开了。", "背包还没打开。"]
        ]
    )
    reviews = iter(
        [
            {"accepted": False, "issues": ["失败不能解释为成功"]},
            {"accepted": True, "issues": []},
        ]
    )
    fake.replies["dialogue"] = lambda _: next(speeches)
    fake.replies["actor_review"] = lambda _: next(reviews)
    runtime.submit(player, config.workspace_owner, "馆长，刚才背包打开了吗？", "scoped-result")
    task = runtime.claim("foreground")
    result = await Agents(db, config, runtime, fake).respond(task)
    assert result["answer"] == "背包还没打开。"
    assert len([c for c in fake.calls if c[0] == "dialogue"]) == 2
    assert len([c for c in fake.calls if c[0] == "actor_review"]) == 2
