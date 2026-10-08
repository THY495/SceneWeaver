import pytest
from pydantic import ValidationError

from chatbot.npc.speech_contract import SPEECH_OUTPUT_CONTRACT
from chatbot.npc.speech_packets import PacketSpeech, output_contract, to_actor_speech
from chatbot.voice import validate_spoken_parts


def test_packets_preserve_order_claims_and_exact_voice_projection():
    claim = {"text": "我不清楚。", "source_id": "profile:x", "quote": "尚不知船班", "mode": "belief"}
    value = to_actor_speech({"segments": [
        {"kind": "action", "text": "停顿"},
        {"kind": "speech", "text": "我不清楚。", "expression": "concerned", "pose": "thinking",
         "voice_direction": "低声"},
        {"kind": "speech", "text": "你怎么看？"},
    ], "claims": [claim]})
    assert value["text"] == "（停顿）我不清楚。你怎么看？"
    assert value["spoken_parts"] == ["我不清楚。", "你怎么看？"]
    assert value["claims"] == [claim]
    assert value["performance"][0]["voice_direction"] == "低声"
    assert value["performance"][0]["text"] == value["spoken_parts"][0]
    validate_spoken_parts(value["text"], value["spoken_parts"])


def test_pure_action_is_not_spoken_and_legacy_contract_is_unchanged():
    value = to_actor_speech({"segments": [{"kind": "action", "text": "沉默"}]})
    assert value["text"] == "（沉默）" and not value["spoken_parts"] and not value["performance"]
    assert output_contract(False) == SPEECH_OUTPUT_CONTRACT
    common = SPEECH_OUTPUT_CONTRACT.split("spoken_parts按顺序", 1)[0].split("每条可核验事实", 1)[1]
    assert common in output_contract(True)
    assert "不得借表现字段加入秘密或执行世界动作。" in output_contract(True)


@pytest.mark.parametrize("segments", [[], [{"text": "x" * 2400}, {"text": "y"}],
    [{"kind": "action", "text": "（点头）"}], [{"kind": "action", "text": "点头", "voice_direction": "低声"}],
    [{"text": "对白", "world_action": "move"}], [{"text": "对白", "expression": "secret"}]])
def test_packet_failures_are_not_silently_trimmed(segments):
    with pytest.raises(ValidationError):
        PacketSpeech.model_validate({"segments": segments})


@pytest.mark.parametrize("accepted", [True, False])
async def test_packet_generation_still_passes_full_review_and_never_publishes_itself(
    db, config, runtime, fake, accepted
):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select
    from support.story import launch

    from chatbot.agents import Agents
    from chatbot.api import create_app
    from chatbot.npc.context import NPCServices, TurnContext
    from chatbot.npc.performance import UnsafeDraftExhausted, perform_actor
    from chatbot.storage import Conversation, SceneRecord

    config.product = "roleplay"
    config.role_speech_representation = "segments"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "你知道吗？", "packet-review")
    task = runtime.claim("test")
    fake.replies["dialogue"] = {"segments": [{"kind": "speech", "text": "我现在还说不准。"}], "claims": []}

    def review(messages):
        import json

        body = json.dumps(messages, ensure_ascii=False)
        assert "我现在还说不准。" in body
        assert "只能扮演" in body and "不能编造关键事实" in body
        return {"accepted": accepted, "issues": [] if accepted else ["事实缺少依据"]}

    fake.replies["actor_review"] = review
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from chatbot.voice_candidate import CandidateAudio

    review_started, audio_started = asyncio.Event(), asyncio.Event()
    original = fake.chat

    async def chat(task_id, role, messages, **kwargs):
        if role == "actor_review":
            review_started.set()
            await asyncio.wait_for(audio_started.wait(), 2)
        return await original(task_id, role, messages, **kwargs)

    async def audio(task, text, voice, direction):
        audio_started.set()
        await asyncio.wait_for(review_started.wait(), 2)
        return CandidateAudio(text, voice, direction, "packet-test", b"\x00\x00")

    fake.chat = chat
    voice = SimpleNamespace(stream_candidate=audio, discard_candidate=AsyncMock())
    agents = Agents(db, config, runtime, fake)
    services = NPCServices(db, config, runtime, fake, agents.context, agents.knowledge)
    with db.session() as s:
        actor = s.get(Conversation, actors[0])
        before = s.scalar(select(func.count()).select_from(SceneRecord))
    call = perform_actor(services, task, TurnContext(story, player, tuple(actors)), actor,
        {"act": "speak", "purpose": "表达当前未知", "retrieval": "context", "addressee_id": player}, 0,
        voice_service=voice, voice="test")
    if accepted:
        result = await call
        assert result["answer"] == "我现在还说不准。"
        assert result["spoken_parts"] == [result["answer"]]
        assert result["_candidate_audio"] is not None
    else:
        with pytest.raises(UnsafeDraftExhausted):
            await call
        voice.discard_candidate.assert_awaited()
    assert any(role == "actor_review" for role, _, _ in fake.calls)
    assert all(kwargs["operation"].endswith(":speech-packets-v1")
               for role, _, kwargs in fake.calls if kwargs.get("schema") is PacketSpeech)
    with db.session() as s:
        assert s.scalar(select(func.count()).select_from(SceneRecord)) == before


async def test_existing_legacy_cache_cannot_be_used_as_packet_output(db, config, runtime, fake):
    import json

    import httpx
    from fastapi.testclient import TestClient
    from support.story import launch

    from chatbot.agents import Agents
    from chatbot.api import create_app
    from chatbot.npc.context import NPCServices, TurnContext
    from chatbot.npc.performance import perform_actor
    from chatbot.providers import Provider, operation_cache_id
    from chatbot.storage import Conversation, Operation

    config.product = "roleplay"
    with TestClient(create_app(config, db, fake)) as client:
        story, actors, player = launch(client)
    runtime.submit(player, config.workspace_owner, "说说你的看法。", "packet-cache")
    task = runtime.claim("test")
    old = {"text": "我还说不准。", "spoken_parts": ["我还说不准。"], "claims": [], "performance": []}
    operation = f"npc:{actors[0]}:0:answer:0"
    with db.session.begin() as s:
        s.add(Operation(id=operation_cache_id(task.id, operation), task_id=task.id, payload={"value": old}))
    provider = Provider(db, config, runtime)
    await provider.http.aclose()
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"segments": [{"kind": "speech", "text": "我现在无法确认。"}], "claims": []})},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 10}})

    provider.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    original = fake.chat

    async def chat(task_id, role, messages, **kwargs):
        if role == "dialogue":
            return await provider.chat(task_id, role, messages, **kwargs)
        return await original(task_id, role, messages, **kwargs)

    fake.chat = chat
    agents = Agents(db, config, runtime, fake)
    services = NPCServices(db, config, runtime, fake, agents.context, agents.knowledge)
    with db.session() as s:
        actor = s.get(Conversation, actors[0])
    intent = {"act": "speak", "purpose": "回应", "retrieval": "context", "addressee_id": player}
    try:
        config.role_speech_representation = "segments"
        packet = await perform_actor(services, task, TurnContext(story, player, tuple(actors)), actor, intent.copy(), 0)
        assert packet["answer"] == "我现在无法确认。" and len(requests) == 1
        config.role_speech_representation = "legacy"
        legacy = await perform_actor(services, task, TurnContext(story, player, tuple(actors)), actor, intent.copy(), 0)
        assert legacy["answer"] == old["text"] and len(requests) == 1
    finally:
        await provider.close()
